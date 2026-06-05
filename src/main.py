import os
import logging
import re
import time
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor
from google.cloud import compute_v1
from google.api_core import exceptions as gcp_exceptions
from dataclasses import dataclass, field
from typing import List, Tuple

logging.basicConfig(level=logging.INFO, format="%(asctime)s - [%(levelname)s] - %(message)s")

@dataclass
class OrchestratorConfig:
    """Centralized configuration loaded from the environment."""
    projects: str = field(default_factory=lambda: os.environ.get("INTERCONNECT_PROJECTS", ""))
    lead_time_minutes: int = field(default_factory=lambda: int(os.environ.get("DRAIN_LEAD_TIME_MINUTES", "60")))
    no_op_policies: bool = field(default_factory=lambda: os.environ.get("NO_OP_POLICIES") == "1")
    import_policy_name: str = "interconnect-maintenance-drain-import"
    export_policy_name: str = "interconnect-maintenance-drain-export"
    wildcard_match_expr: str = "destination.inAnyRange([prefix('0.0.0.0/0').orLonger(), prefix('::/0').orLonger()])"

config = OrchestratorConfig()

@dataclass
class BgpPeerTarget:
    """Represents a targeted BGP peer and its desired state."""
    project_id: str
    region: str
    router_name: str
    peer_name: str
    target_policy_state: str
    is_drained_currently: bool

@dataclass
class InterconnectAuditResult:
    """Represents the audit result of an interconnect."""
    project_id: str
    interconnect: str
    outage_id: str
    target_state: str
    current_state: str
    action: str
    status: str
    associated_routers: List[Tuple[str, str, str]] = field(default_factory=list)
    _peer_targets: List[BgpPeerTarget] = field(default_factory=list)

# Compute Engine API Clients
interconnects_client = compute_v1.InterconnectsClient()
attachments_client = compute_v1.InterconnectAttachmentsClient()
routers_client = compute_v1.RoutersClient()

def get_routers_client():
    return compute_v1.RoutersClient()

def process_maintenance_events(target_projects: str = ""):
    logging.info(f"Maintenance Events Check Loop Started at {datetime.now(timezone.utc).isoformat()}")
    
    projects_to_use = target_projects if target_projects else config.projects
    if not projects_to_use:
        error_msg = "CRITICAL: Maintenance events loop aborted. No target projects specified (via argument or INTERCONNECT_PROJECTS env var)."
        logging.error(error_msg)
        raise ValueError(error_msg)

    project_list = list(dict.fromkeys(p.strip() for p in projects_to_use.split(",") if p.strip()))
    logging.info(f"Targeted networks maintenance events check scoped to project list: {project_list}")
    
    run_summary, routers_to_align, failed_projects = _audit_projects(project_list)
    filtered_routers_to_align = _evaluate_and_consolidate(run_summary, routers_to_align)
    router_alignment_results = _align_routers_parallel(filtered_routers_to_align, failed_projects)
    _update_final_statuses(run_summary, router_alignment_results)
    
    log_sre_summary_table(run_summary)

    if failed_projects:
         raise RuntimeError(f"CRITICAL: Maintenance events completed with failures on projects: {list(failed_projects)}")

    logging.info("All target network project maintenance events checks completed successfully.")

def process_interconnect_maintenance_events(
    project_id: str, 
    ic: compute_v1.Interconnect, 
    router_cache: dict
) -> Tuple[InterconnectAuditResult, List[BgpPeerTarget]]:
    ic_name = ic.name
    logging.info(f"Auditing Interconnect: '{ic_name}' [Project: {project_id}]")

    outages = list(ic.expected_outages) if ic.expected_outages else []
    logging.info(f"Found {len(outages)} planned outage notifications scheduled for '{ic_name}'")

    now = datetime.now(timezone.utc)
    active_outages = []

    for outage in outages:
         state = outage.state
         start_time = parse_epoch_ms(outage.start_time)
         end_time = parse_epoch_ms(outage.end_time)

         if not start_time or not end_time:
              logging.warning(f"Outage notification {outage.name} contains invalid time format. Skipping.")
              continue

         drain_start = start_time - timedelta(minutes=config.lead_time_minutes)
         is_in_drain_window = (now >= drain_start) and (now < end_time)
         
         if state == "ACTIVE" and is_in_drain_window:
              active_outages.append({
                   "id": outage.name,
                   "start_time": start_time.isoformat(),
                   "end_time": end_time.isoformat(),
                   "description": outage.description
              })
              logging.info(f"Target Active Outage matched: {outage.name} (Start: {start_time.isoformat()}, End: {end_time.isoformat()})")

    target_state = "DRAINED" if len(active_outages) > 0 else "NORMAL"
        
    attachments = list(ic.interconnect_attachments) if ic.interconnect_attachments else []
    if not attachments:
         logging.info(f"No VLAN attachments mapped to Interconnect {ic_name}. Skipping routing state audits.")
         result = InterconnectAuditResult(
             project_id=project_id,
             interconnect=ic_name,
             outage_id=active_outages[0]["id"] if active_outages else "NONE",
             target_state=target_state,
             current_state="N/A",
             action="NO_ACTION",
             status="SUCCESS: NO_ATTACHMENTS"
         )
         return result, []

    peer_states = check_current_bgp_states(attachments, router_cache)
    current_state = "DRAINED" if any(p["is_drained"] for p in peer_states) else "NORMAL"
    associated_routers = list(set((p["project_id"], p["region"], p["router_name"]) for p in peer_states))
    
    logging.info(f"Interconnect '{ic_name}' [Project: {project_id}] -> TargetState: {target_state}, CurrentState: {current_state}")

    peer_targets = []
    for p in peer_states:
         peer_targets.append(BgpPeerTarget(
              project_id=p["project_id"],
              region=p["region"],
              router_name=p["router_name"],
              peer_name=p["peer_name"],
              target_policy_state=target_state,
              is_drained_currently=p["is_drained"]
         ))

    result = InterconnectAuditResult(
        project_id=project_id,
        interconnect=ic_name,
        outage_id=active_outages[0]["id"] if active_outages else "NONE",
        target_state=target_state,
        current_state=current_state,
        action="NO_ACTION",
        status="SUCCESS",
        associated_routers=associated_routers
    )
    return result, peer_targets

def check_current_bgp_states(attachments: list, router_cache: dict) -> list:
    peer_states = []
    
    for attach_url in attachments:
        try:
             proj, region, name = parse_attachment_url(attach_url)
             attach_data = attachments_client.get(project=proj, region=region, interconnect_attachment=name)
             router_url = attach_data.router
             if not router_url:
                  logging.warning(f"VLAN attachment {name} has no associated Cloud Router. Skipping.")
                  continue
             
             router_name = router_url.split("/")[-1]
             
             cache_key = (proj, region, router_name)
             if cache_key not in router_cache:
                  router = routers_client.get(project=proj, region=region, router=router_name)
                  router_cache[cache_key] = router
             router = router_cache[cache_key]
             
             target_interface = None
             for interface in router.interfaces:
                 attachment_ref = getattr(interface, "linked_interconnect_attachment", "")
                 if attachment_ref and attachment_ref.split("/")[-1] == name:
                      target_interface = interface.name
                      break
             
             if not target_interface:
                  continue

             for peer in router.bgp_peers:
                  if peer.interface_name == target_interface:
                       is_import_drained = config.import_policy_name in peer.import_policies
                       is_export_drained = config.export_policy_name in peer.export_policies
                       
                       peer_states.append({
                            "project_id": proj,
                            "region": region,
                            "router_name": router_name,
                            "peer_name": peer.name,
                            "attachment_name": name,
                            "is_drained": is_import_drained and is_export_drained,
                            "original_import": list(peer.import_policies),
                            "original_export": list(peer.export_policies)
                       })
        except gcp_exceptions.Forbidden as e:
             logging.error(f"CRITICAL: IAM PERMISSION DENIED auditing BGP sessions on attachment {attach_url}. Missing 'compute.interconnectAttachments.get' or 'compute.routers.get' in the target project. Details: {e}")
             raise
        except Exception as e:
             logging.error(f"Error auditing BGP sessions on attachment {attach_url}: {e}")
             raise
             
    return peer_states

def toggle_drain_policies(peer, enable_drain: bool):
    """Cleanly modifies BGP peer route policies to enable or disable the drain state."""
    original_import = list(peer.import_policies)
    original_export = list(peer.export_policies)
    
    if enable_drain:
        new_import = [config.import_policy_name] + [x for x in original_import if x != config.import_policy_name]
        new_export = [config.export_policy_name] + [x for x in original_export if x != config.export_policy_name]
    else:
        new_import = [x for x in original_import if x != config.import_policy_name]
        new_export = [x for x in original_export if x != config.export_policy_name]

    peer.import_policies.clear()
    peer.import_policies.extend(new_import)
    peer.export_policies.clear()
    peer.export_policies.extend(new_export)

def safely_patch_router(router_key: tuple, peer_mods: List[BgpPeerTarget]):
    proj, region, router_name = router_key
    local_routers_client = get_routers_client()

    logging.info(f"Beginning thread-safe write alignment for Cloud Router '{router_name}' under Project '{proj}'")
    
    max_attempts = 5
    attempt = 0
    
    while attempt < max_attempts:
         try:
              router = local_routers_client.get(project=proj, region=region, router=router_name)
              policies_patched = ensure_drain_policies_exist(proj, region, router, local_routers_client)
              
              if policies_patched:
                   router = local_routers_client.get(project=proj, region=region, router=router_name)
              
              for mod in peer_mods:
                   for peer in router.bgp_peers:
                        if peer.name == mod.peer_name:
                             if mod.target_policy_state == "DRAINED":
                                  toggle_drain_policies(peer, enable_drain=True)
                                  logging.info(f"Thread injecting Drain Route Policies into BGP peer '{mod.peer_name}' on Router '{router_name}'")
                             else:
                                  toggle_drain_policies(peer, enable_drain=False)
                                  logging.info(f"Thread stripping Drain Route Policies from BGP peer '{mod.peer_name}' on Router '{router_name}'")

              operation = local_routers_client.patch(
                  project=proj,
                  region=region,
                  router=router_name,
                  router_resource=router
              )
              
              logging.info(f"PATCH operation '{operation.name}' dispatched. Waiting for completion...")
              operation.result() 
              logging.info(f"PATCH operation '{operation.name}' completed successfully.")
              return 

         except (gcp_exceptions.PreconditionFailed, gcp_exceptions.TooManyRequests, gcp_exceptions.InternalServerError) as e:
              attempt += 1
              logging.warning(f"Transient or concurrency error on attempt {attempt}: {e}. Retrying with backoff...")
              if attempt >= max_attempts:
                   raise RuntimeError(f"Unable to reconcile patch updates on Router {router_name} after {max_attempts} attempts. Error: {e}")
              time.sleep(2 ** attempt)
         except gcp_exceptions.Forbidden as e:
              raise RuntimeError(f"CRITICAL: IAM PERMISSION DENIED patching Cloud Router '{router_name}' [Project: {proj}]. Missing 'compute.routers.update' on the target resource. Details: {e}") from e
         except Exception as e:
              raise RuntimeError(f"CRITICAL: Non-retryable error applying patch to Router {router_name}: {e}")

def _is_term_valid(term: compute_v1.RoutePolicyPolicyTerm, expected_actions: List[str]) -> bool:
    if term.priority != 1:
        return False
    if not term.match or term.match.expression != config.wildcard_match_expr:
        return False
    
    term_actions = [a.expression for a in term.actions]
    return term_actions == expected_actions

def _upsert_policy(
    project_id: str, 
    region: str, 
    router_name: str, 
    name: str, 
    policy_type: str, 
    expected_actions: List[str], 
    existing_policy: compute_v1.RoutePolicy, 
    local_routers_client
) -> bool:
    action_exprs = [compute_v1.Expr(expression=act) for act in expected_actions]
    match_expr = compute_v1.Expr(expression=config.wildcard_match_expr)

    term = compute_v1.RoutePolicyPolicyTerm(
        priority=1,
        match=match_expr,
        actions=action_exprs
    )

    need_patch = False
    if existing_policy:
        is_valid = (
            existing_policy.type_ == policy_type and
            len(existing_policy.terms) == 1 and
            _is_term_valid(existing_policy.terms[0], expected_actions)
        )
        if not is_valid:
            logging.warning(f"Route policy '{name}' was tampered with or outdated. Re-reconciling.")
            need_patch = True
    else:
        logging.info(f"Route policy '{name}' does not exist. Creating.")
        need_patch = True

    if need_patch:
        policy_resource = compute_v1.RoutePolicy(
            name=name,
            type_=policy_type,
            terms=[term]
        )
        if existing_policy and getattr(existing_policy, "fingerprint", None):
            policy_resource.fingerprint = existing_policy.fingerprint
        else:
            policy_resource.fingerprint = "" 

        try:
            operation = local_routers_client.update_route_policy(
                project=project_id,
                region=region,
                router=router_name,
                route_policy_resource=policy_resource
            )
            logging.info(f"UPDATE route policy '{name}' operation '{operation.name}' dispatched. Waiting...")
            operation.result()
            logging.info(f"UPDATE route policy '{name}' completed successfully.")
            return True
        except Exception as e:
            logging.error(f"Failed to update route policy '{name}' on router {router_name}: {e}")
            raise
    return False

def ensure_drain_policies_exist(project_id: str, region: str, router: compute_v1.Router, routers_client) -> bool:
    if config.no_op_policies:
        expected_import_actions = ["nextPolicy()"]
        expected_export_actions = ["nextPolicy()"]
    else:
        expected_import_actions = [
             "med.add(65535)",
             f"asPath.prependSequence([{router.bgp.asn}, {router.bgp.asn}, {router.bgp.asn}, {router.bgp.asn}])"
        ]
        expected_export_actions = [
             f"asPath.prependSequence([{router.bgp.asn}, {router.bgp.asn}, {router.bgp.asn}, {router.bgp.asn}])"
        ]

    logging.info(f"Reconciling route policies for router '{router.name}'. Expected import actions: {expected_import_actions}, export actions: {expected_export_actions}")

    try:
        existing_policies = list(routers_client.list_route_policies(project=project_id, region=region, router=router.name))
    except Exception as e:
        logging.error(f"Error listing route policies for router {router.name}: {e}")
        raise

    import_policy = next((p for p in existing_policies if p.name == config.import_policy_name), None)
    export_policy = next((p for p in existing_policies if p.name == config.export_policy_name), None)

    import_patched = _upsert_policy(project_id, region, router.name, config.import_policy_name, "ROUTE_POLICY_TYPE_IMPORT", expected_import_actions, import_policy, routers_client)
    export_patched = _upsert_policy(project_id, region, router.name, config.export_policy_name, "ROUTE_POLICY_TYPE_EXPORT", expected_export_actions, export_policy, routers_client)

    return import_patched or export_patched

def _audit_projects(project_list: list) -> tuple:
    run_summary = []
    routers_to_align = {}
    router_cache = {}
    failed_projects = set()

    for project_id in project_list:
        logging.info(f"Scanning physical Interconnect resources under Project '{project_id}'")
        try:
            interconnects = list(interconnects_client.list(project=project_id))
            logging.info(f"Discovered {len(interconnects)} physical links inside project '{project_id}'")
            
            for ic in interconnects:
                try:
                     record, peer_targets = process_interconnect_maintenance_events(
                         project_id, ic, router_cache
                     )
                     record._peer_targets = peer_targets 
                     run_summary.append(record)
                     
                     if peer_targets:
                          associated_routers = set()
                          for target in peer_targets:
                               rkey = (target.project_id, target.region, target.router_name)
                               associated_routers.add(rkey)
                               
                               if rkey not in routers_to_align:
                                    routers_to_align[rkey] = {}
                               
                               peer_name = target.peer_name
                               if peer_name not in routers_to_align[rkey]:
                                    routers_to_align[rkey][peer_name] = target
                               else:
                                    existing = routers_to_align[rkey][peer_name]
                                    if target.target_policy_state == "DRAINED":
                                         existing.target_policy_state = "DRAINED"
                                         
                          record.associated_routers = list(associated_routers)
                except gcp_exceptions.Forbidden as e:
                     error_details = f"CRITICAL: IAM PERMISSION DENIED processing physical link '{ic.name}' under project '{project_id}'. The orchestrator Service Account is missing required read permissions. Details: {e}"
                     logging.error(error_details, exc_info=True)
                     run_summary.append(InterconnectAuditResult(
                         project_id=project_id, interconnect=ic.name, outage_id="UNKNOWN",
                         target_state="UNKNOWN", current_state="UNKNOWN", action="ERROR", status="FAILED: IAM_PERMISSION_DENIED"
                     ))
                     failed_projects.add(project_id)
                except Exception as e:
                     error_details = f"CRITICAL: Maintenance events processing failure on link '{ic.name}' under project '{project_id}': {str(e)}"
                     logging.error(error_details, exc_info=True)
                     run_summary.append(InterconnectAuditResult(
                         project_id=project_id, interconnect=ic.name, outage_id="UNKNOWN",
                         target_state="UNKNOWN", current_state="UNKNOWN", action="ERROR", status=f"FAILED: {str(e)[:500]}"
                     ))
                     failed_projects.add(project_id)
                      
        except gcp_exceptions.Forbidden as e:
            error_details = f"CRITICAL: IAM PERMISSION DENIED listing project '{project_id}'. The orchestrator Service Account is missing 'compute.interconnects.list'. Details: {e}"
            logging.error(error_details, exc_info=True)
            run_summary.append(InterconnectAuditResult(
                project_id=project_id, interconnect="ALL_LINKS", outage_id="N/A",
                target_state="N/A", current_state="N/A", action="ERROR", status="FAILED: IAM_PERMISSION_DENIED"
            ))
            failed_projects.add(project_id)
        except Exception as e:
            error_details = f"CRITICAL: Maintenance events loop failure listing project '{project_id}': {str(e)}"
            logging.error(error_details, exc_info=True)
            run_summary.append(InterconnectAuditResult(
                project_id=project_id, interconnect="ALL_LINKS", outage_id="N/A",
                target_state="N/A", current_state="N/A", action="ERROR", status=f"FAILED: {str(e)[:500]}"
            ))
            failed_projects.add(project_id)
            
    return run_summary, routers_to_align, failed_projects

def _evaluate_and_consolidate(run_summary: List[InterconnectAuditResult], routers_to_align: dict) -> dict:
    filtered_routers_to_align = {}
    for rkey, mods_dict in routers_to_align.items():
         needs_alignment = False
         for peer_name, target in mods_dict.items():
              if not is_peer_aligned(target.target_policy_state, target.is_drained_currently):
                   needs_alignment = True
                   break
         if needs_alignment:
              filtered_routers_to_align[rkey] = mods_dict

    for record in run_summary:
         if not record._peer_targets:
              continue
         
         peer_targets = record._peer_targets
         has_delta = False
         ic_target_state = record.target_state
         
         for target in peer_targets:
              rkey = (target.project_id, target.region, target.router_name)
              peer_name = target.peer_name
              
              consolidated_target = routers_to_align[rkey][peer_name].target_policy_state
              is_drained = target.is_drained_currently
              
              if not is_peer_aligned(consolidated_target, is_drained):
                   has_delta = True
                   ic_target_state = consolidated_target
                   break
         
         if has_delta:
              record.action = "DRAINED" if ic_target_state == "DRAINED" else "RESTORED"
              record.status = "PENDING_ALIGNMENT"
         else:
              record.action = "NO_ACTION"
              record.status = "SUCCESS"
          
         record._peer_targets = []
         
    return filtered_routers_to_align

def _align_routers_parallel(filtered_routers_to_align: dict, failed_projects: set) -> dict:
    router_alignment_results = {}
    write_futures = {}
    
    if filtered_routers_to_align:
         logging.info(f"Unified Router Ledger prepared with {len(filtered_routers_to_align)} distinct Cloud Routers to align.")
         
         with ThreadPoolExecutor(max_workers=4) as write_executor:
              for rkey, mods_dict in filtered_routers_to_align.items():
                   mods = list(mods_dict.values())
                   future = write_executor.submit(safely_patch_router, rkey, mods)
                   write_futures[future] = rkey

              logging.info(f"Waiting for {len(write_futures)} Cloud Router alignment tasks to complete (Max 4 concurrent)...")
              for future, rkey in write_futures.items():
                   proj, region, router_name = rkey
                   try:
                        future.result()
                        router_alignment_results[rkey] = {"success": True, "error": None}
                        logging.info(f"Successfully aligned Cloud Router '{router_name}' [Project: {proj}]")
                   except Exception as e:
                        error_msg = str(e)[:500]
                        router_alignment_results[rkey] = {"success": False, "error": error_msg}
                        logging.error(f"CRITICAL: Failed aligning Cloud Router '{router_name}': {e}", exc_info=True)
                        failed_projects.add(proj)
                        
    return router_alignment_results

def _update_final_statuses(run_summary: List[InterconnectAuditResult], router_alignment_results: dict):
    for record in run_summary:
         if record.status == "PENDING_ALIGNMENT":
              failed_router = next((r for r in record.associated_routers if r in router_alignment_results and not router_alignment_results[r]["success"]), None)
              if failed_router:
                   err = router_alignment_results[failed_router]["error"]
                   record.status = f"FAILED: {err}"
                   record.action = "ERROR"
              else:
                   record.status = "SUCCESS"

def is_peer_aligned(target_state: str, is_drained: bool) -> bool:
    return (target_state == "DRAINED" and is_drained) or (target_state == "NORMAL" and not is_drained)

# --- UTILS & FORMATTERS ---

def parse_epoch_ms(ts_val) -> datetime:
    if not ts_val:
         return None
    try:
         return datetime.fromtimestamp(int(ts_val) / 1000.0, tz=timezone.utc)
    except (ValueError, TypeError) as e:
         raise ValueError(f"CRITICAL: Unable to parse maintenance timestamp '{ts_val}'. Schema may have changed: {e}")

def parse_attachment_url(url: str) -> Tuple[str, str, str]:
    pattern = r"/projects/([^/]+)/regions/([^/]+)/interconnectAttachments/([^/]+)$"
    match = re.search(pattern, url)
    if not match:
        raise ValueError(f"Unrecognized Interconnect Attachment resource URI structure: {url}")
    return match.group(1), match.group(2), match.group(3)

def log_sre_summary_table(records: List[InterconnectAuditResult]):
    if not records:
        logging.info("\n### GCI MAINTENANCE EVENTS SUMMARY: ZERO INTERCONNECTS DETECTED")
        return

    lines = []
    lines.append("\n### GCI Routing Maintenance Events Summary Run")
    lines.append("| PROJECT ID | INTERCONNECT | ACTIVE OUTAGE | TARGET STATE | CURRENT STATE | ACTION TAKEN | RESULT STATUS |")
    lines.append("| :--- | :--- | :--- | :--- | :--- | :--- | :--- |")
    
    for r in records:
         row = "| {} | {} | {} | {} | {} | {} | {} |".format(
             r.project_id,
             r.interconnect,
             r.outage_id,
             r.target_state,
             r.current_state,
             r.action,
             r.status
         )
         lines.append(row)
         
    lines.append("\n")
    logging.info("\n".join(lines))

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Drain/undrain Interconnect connections before/after maintenance events, by applying bgp routing policy on affected bgp sessions")
    parser.add_argument(
        "--projects",
        default="",
        help="Optional: Comma-separated list of GCP Project IDs to scan. If omitted, falls back to config.projects."
    )
    args = parser.parse_args()
    process_maintenance_events(target_projects=args.projects)

