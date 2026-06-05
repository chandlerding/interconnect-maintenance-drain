import os
import logging
import re
import time
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor
from google.cloud import compute_v1
from google.api_core import exceptions as gcp_exceptions

logging.basicConfig(level=logging.INFO, format="%(asctime)s - [%(levelname)s] - %(message)s")

# Global Configuration settings (retrieved from serverless environment)
INTERCONNECT_PROJECTS = os.environ.get("INTERCONNECT_PROJECTS", "") # Comma-separated list of host project IDs
DRAIN_LEAD_TIME_MINUTES = int(os.environ.get("DRAIN_LEAD_TIME_MINUTES", "60")) # Pre-maintenance lead time (minutes)

# Route Policy Prefix and Names (modify here to customize)
POLICY_PREFIX = "interconnect-maintenance-drain-"
IMPORT_POLICY_NAME = f"{POLICY_PREFIX}import"
EXPORT_POLICY_NAME = f"{POLICY_PREFIX}export"

# Compute Engine API Clients
interconnects_client = compute_v1.InterconnectsClient()
attachments_client = compute_v1.InterconnectAttachmentsClient()
routers_client = compute_v1.RoutersClient()

def get_routers_client():
    return compute_v1.RoutersClient()

def get_operations_client():
    return compute_v1.RegionOperationsClient()

def process_maintenance_events(
    target_projects: str = ""
):
    """
    Main entry point triggered on execution.
    Performs declarative, self-healing state reconciliation on all interconnect routing
    across multiple targeted networking host projects, producing a markdown summary run table.
    All errors are logged directly to stderr, triggering native log-based alerts.
    Runs all GET/LIST API read operations sequentially to conserve API quota,
    while executing slow Cloud Router PATCH write operations concurrently, capped at 4 writes max.
    Guarantees no race conditions on Cloud Routers using a Unified Router Ledger pattern.
    """
    logging.info(f"Maintenance Events Check Loop Started at {datetime.now(timezone.utc).isoformat()}")
    
    projects_to_use = target_projects if target_projects else INTERCONNECT_PROJECTS
    if not projects_to_use:
        error_msg = "CRITICAL: Maintenance events loop aborted. No target projects specified (via argument or INTERCONNECT_PROJECTS env var)."
        logging.error(error_msg)
        raise ValueError(error_msg)

    project_list = list(dict.fromkeys(p.strip() for p in projects_to_use.split(",") if p.strip()))
    logging.info(f"Targeted networks maintenance events check scoped to project list: {project_list}")
    
    # 1. Audit Phase: Scan all projects and build the routing alignment ledger
    run_summary, routers_to_align, failed_projects = _audit_projects(project_list)
    
    # 1.5. Consolidated Evaluation Phase: Resolve target states and identify unaligned routers
    filtered_routers_to_align = _evaluate_and_consolidate(run_summary, routers_to_align)
    
    # 2. Parallel Patch Phase: Execute slow Cloud Router updates in parallel
    router_alignment_results = _align_routers_parallel(filtered_routers_to_align, failed_projects)
    
    # 3. Final Status Update Phase: Update Interconnect statuses based on BGP outcomes
    _update_final_statuses(run_summary, router_alignment_results)
    
    # Construct and print standard Markdown summary run table for SRE review
    log_sre_summary_table(run_summary)

    if failed_projects:
         raise RuntimeError(f"CRITICAL: Maintenance events completed with failures on projects: {list(failed_projects)}")

    logging.info("All target network project maintenance events checks completed successfully.")

def process_interconnect_maintenance_events(
    project_id: str, 
    ic: compute_v1.Interconnect, 
    router_cache: dict
) -> tuple:
    ic_name = ic.name
    logging.info(f"Auditing Interconnect: '{ic_name}' [Project: {project_id}]")

    outages = list(ic.expected_outages) if ic.expected_outages else []
    logging.info(f"Found {len(outages)} planned outage notifications scheduled for '{ic_name}'")

    now = datetime.now(timezone.utc)
    active_outages = []

    # 1. Inspect planned expectedOutages to identify if a drain window is active
    for outage in outages:
         state = outage.state
         start_time = parse_epoch_ms(outage.start_time)
         end_time = parse_epoch_ms(outage.end_time)

         if not start_time or not end_time:
              logging.warning(f"Outage notification {outage.name} contains invalid time format. Skipping.")
              continue

         # Compute drain window trigger threshold
         drain_start = start_time - timedelta(minutes=DRAIN_LEAD_TIME_MINUTES)
         is_in_drain_window = (now >= drain_start) and (now < end_time)
         
         if state == "ACTIVE" and is_in_drain_window:
              active_outages.append({
                   "id": outage.name,
                   "start_time": start_time.isoformat(),
                   "end_time": end_time.isoformat(),
                   "description": outage.description
              })
              logging.info(f"Target Active Outage matched: {outage.name} (Start: {start_time.isoformat()}, End: {end_time.isoformat()})")

    # Target state: DRAINED if at least one maintenance outage is ongoing/imminent, else NORMAL
    target_state = "DRAINED" if len(active_outages) > 0 else "NORMAL"
        
    attachments = list(ic.interconnect_attachments) if ic.interconnect_attachments else []
    if not attachments:
         logging.info(f"No VLAN attachments mapped to Interconnect {ic_name}. Skipping routing state audits.")
         return {
             "project_id": project_id,
             "interconnect": ic_name,
             "outage_id": active_outages[0]["id"] if active_outages else "NONE",
             "target_state": target_state,
             "current_state": "N/A",
             "action": "NO_ACTION",
             "status": "SUCCESS: NO_ATTACHMENTS",
             "associated_routers": []
         }, []

    # 2. Get current BGP session routing status directly from Routers
    peer_states = check_current_bgp_states(attachments, router_cache)
    
    # Current state: DRAINED if even ONE session contains our drain policies, else NORMAL
    current_state = "DRAINED" if any(p["is_drained"] for p in peer_states) else "NORMAL"
    
    # Extract all associated routers regardless of state alignment requirements
    associated_routers = list(set((p["project_id"], p["region"], p["router_name"]) for p in peer_states))
    
    logging.info(f"Interconnect '{ic_name}' [Project: {project_id}] -> TargetState: {target_state}, CurrentState: {current_state}")

    # Formulate raw target states for all BGP peers associated with this Interconnect.
    # The main loop will consolidate these targets and check against live states to determine the final actions.
    peer_targets = []
    for p in peer_states:
         peer_targets.append({
              "project_id": p["project_id"],
              "region": p["region"],
              "router_name": p["router_name"],
              "peer_name": p["peer_name"],
              "target_policy_state": target_state,
              "is_drained_currently": p["is_drained"]
         })

    return {
        "project_id": project_id,
        "interconnect": ic_name,
        "outage_id": active_outages[0]["id"] if active_outages else "NONE",
        "target_state": target_state,
        "current_state": current_state,
        "action": "NO_ACTION", # Will be updated in the consolidated evaluation phase
        "status": "SUCCESS",     # Will be updated in the consolidated evaluation phase
        "associated_routers": associated_routers
    }, peer_targets

def check_current_bgp_states(attachments: list, router_cache: dict) -> list:
    """Audits each logical BGP peering session state directly from Cloud Routers configuration."""
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
             
             # Match interface associated to target attachment name
             target_interface = None
             for interface in router.interfaces:
                 attachment_ref = getattr(interface, "linked_interconnect_attachment", "")
                 if attachment_ref and attachment_ref.split("/")[-1] == name:
                      target_interface = interface.name
                      break
             
             if not target_interface:
                  continue

             # Audit BGP peers
             for peer in router.bgp_peers:
                  if peer.interface_name == target_interface:
                       import_policy_name = IMPORT_POLICY_NAME
                       export_policy_name = EXPORT_POLICY_NAME
                       is_import_drained = import_policy_name in peer.import_policies
                       is_export_drained = export_policy_name in peer.export_policies
                       
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

def safely_patch_router(router_key: tuple, peer_mods: list):
    """
    Executes Cloud Router Route Policies injection or removal in a thread-safe manner.
    Instantiates its own local Compute Client library to avoid shared transport thread state issues.
    Uses an atomic fetch-modify-patch retry loop to safely handle optimistic concurrency conflicts (fingerprint).
    Uses the standard GAPIC operation.result() block waiter to optimize API quota usage.
    """
    proj, region, router_name = router_key
    
    # Thread-safe local client instantiations via factories to prevent parallel gRPC transport issues
    local_routers_client = get_routers_client()

    logging.info(f"Beginning thread-safe write alignment for Cloud Router '{router_name}' under Project '{proj}'")
    
    max_attempts = 5
    attempt = 0
    
    while attempt < max_attempts:
         try:
              # 1. Fetch a fresh copy of the Cloud Router (guarantees fresh ETag fingerprint)
              router = local_routers_client.get(project=proj, region=region, router=router_name)
              
              # 2. Ensure GCI drain route policies exist globally in GCP for this router
              policies_patched = ensure_drain_policies_exist(proj, region, router, local_routers_client)
              
              if policies_patched:
                   router = local_routers_client.get(project=proj, region=region, router=router_name)
              
              # 3. Apply the BGP peer modifications atomically to the fresh router object
              for mod in peer_mods:
                   peer_name = mod["peer_name"]
                   target_state = mod["target_policy_state"]
                   
                   for peer in router.bgp_peers:
                        if peer.name == peer_name:
                             original_import = list(peer.import_policies)
                             original_export = list(peer.export_policies)
                             
                             if target_state == "DRAINED":
                                  new_import = [IMPORT_POLICY_NAME] + [x for x in original_import if x != IMPORT_POLICY_NAME]
                                  new_export = [EXPORT_POLICY_NAME] + [x for x in original_export if x != EXPORT_POLICY_NAME]
                                  peer.import_policies.clear()
                                  peer.import_policies.extend(new_import)
                                  peer.export_policies.clear()
                                  peer.export_policies.extend(new_export)
                                  logging.info(f"Thread injecting Drain Route Policies into BGP peer '{peer_name}' on Router '{router_name}'")
                             else:
                                  peer.import_policies.clear()
                                  peer.import_policies.extend([x for x in original_import if x != IMPORT_POLICY_NAME])
                                  peer.export_policies.clear()
                                  peer.export_policies.extend([x for x in original_export if x != EXPORT_POLICY_NAME])
                                  logging.info(f"Thread stripping Drain Route Policies from BGP peer '{peer_name}' on Router '{router_name}'")

              # 4. Perform PATCH (implicitly passes the loaded ETag fingerprint for optimistic concurrency control)
              operation = local_routers_client.patch(
                  project=proj,
                  region=region,
                  router=router_name,
                  router_resource=router
              )
              
              # 5. Leverage SDK's native block waiter to wait for completion (Quota-friendly)
              logging.info(f"PATCH operation '{operation.name}' dispatched. Waiting for completion...")
              operation.result() # Blocks and handles backoffs internally. Raises errors on failure.
              logging.info(f"PATCH operation '{operation.name}' completed successfully.")
              return # Successful patch, exit loop!

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

def ensure_drain_policies_exist(project_id: str, region: str, router: compute_v1.Router, routers_client) -> bool:
    import_policy_name = IMPORT_POLICY_NAME
    export_policy_name = EXPORT_POLICY_NAME
    wildcard_match_expr = "destination.inAnyRange([prefix('0.0.0.0/0').orLonger(), prefix('::/0').orLonger()])"

    if os.environ.get("NO_OP_POLICIES") == "1":
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

    # 1. List existing policies in GCP
    try:
        existing_policies = list(routers_client.list_route_policies(project=project_id, region=region, router=router.name))
    except Exception as e:
        logging.error(f"Error listing route policies for router {router.name}: {e}")
        raise

    import_policy = None
    export_policy = None
    for policy in existing_policies:
        if policy.name == import_policy_name:
            import_policy = policy
        elif policy.name == export_policy_name:
            export_policy = policy

    # Helper to validate term
    def is_term_valid(term, expected_actions):
        if term.priority != 1:
            return False
        if not term.match or term.match.expression != wildcard_match_expr:
            return False
        
        term_actions = [a.expression for a in term.actions]
        return term_actions == expected_actions

    # Helper to create/patch policy
    def upsert_policy(name, policy_type, expected_actions, existing_policy) -> bool:
        action_exprs = [compute_v1.Expr(expression=act) for act in expected_actions]
        match_expr = compute_v1.Expr(expression=wildcard_match_expr)

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
                is_term_valid(existing_policy.terms[0], expected_actions)
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
            if existing_policy and existing_policy.fingerprint:
                policy_resource.fingerprint = existing_policy.fingerprint
            else:
                policy_resource.fingerprint = "" # Required by GCP API even for creation

            try:
                operation = routers_client.update_route_policy(
                    project=project_id,
                    region=region,
                    router=router.name,
                    route_policy_resource=policy_resource
                )
                logging.info(f"UPDATE route policy '{name}' operation '{operation.name}' dispatched. Waiting...")
                operation.result()
                logging.info(f"UPDATE route policy '{name}' completed successfully.")
                return True
            except Exception as e:
                logging.error(f"Failed to update route policy '{name}' on router {router.name}: {e}")
                raise
        return False

    # Reconcile import policy
    import_patched = upsert_policy(import_policy_name, "ROUTE_POLICY_TYPE_IMPORT", expected_import_actions, import_policy)

    # Reconcile export policy
    export_patched = upsert_policy(export_policy_name, "ROUTE_POLICY_TYPE_EXPORT", expected_export_actions, export_policy)

    return import_patched or export_patched



def _audit_projects(project_list: list) -> tuple:
    """Scans all projects and physical links sequentially to build the routing alignment ledger.
    
    Args:
        project_list: List of project IDs to scan.
        
    Returns:
        tuple: (run_summary, routers_to_align, failed_projects)
            - run_summary (list): List of dicts summarizing each Interconnect's status.
            - routers_to_align (dict): Ledger of routers and BGP peers that need alignment.
            - failed_projects (set): Set of project IDs that encountered errors during audit.
    """
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
                     # Process checks sequentially (fast reads). Returns summary record and list of raw peer targets
                     record, peer_targets = process_interconnect_maintenance_events(
                         project_id, ic, router_cache
                     )
                     record["_peer_targets"] = peer_targets # Store temporarily for consolidated evaluation
                     run_summary.append(record)
                     
                     if peer_targets:
                          # Record routers this Interconnect record depends on for final SRE status mapping
                          associated_routers = set()
                          for target in peer_targets:
                               rkey = (target["project_id"], target["region"], target["router_name"])
                               associated_routers.add(rkey)
                               
                               if rkey not in routers_to_align:
                                    routers_to_align[rkey] = {}
                               
                               peer_name = target["peer_name"]
                               # Deduplication & Precedence: If a peer has conflicting intents, DRAINED takes absolute precedence!
                               if peer_name not in routers_to_align[rkey]:
                                    routers_to_align[rkey][peer_name] = target
                               else:
                                    existing = routers_to_align[rkey][peer_name]
                                    if target["target_policy_state"] == "DRAINED":
                                         existing["target_policy_state"] = "DRAINED"
                                         
                          record["associated_routers"] = list(associated_routers)
                except gcp_exceptions.Forbidden as e:
                     error_details = f"CRITICAL: IAM PERMISSION DENIED processing physical link '{ic.name}' under project '{project_id}'. The orchestrator Service Account is missing required read permissions. Details: {e}"
                     logging.error(error_details, exc_info=True)
                     run_summary.append({
                         "project_id": project_id,
                         "interconnect": ic.name,
                         "outage_id": "UNKNOWN",
                         "target_state": "UNKNOWN",
                         "current_state": "UNKNOWN",
                         "action": "ERROR",
                         "status": "FAILED: IAM_PERMISSION_DENIED"
                     })
                     failed_projects.add(project_id)
                except Exception as e:
                     error_details = f"CRITICAL: Maintenance events processing failure on link '{ic.name}' under project '{project_id}': {str(e)}"
                     logging.error(error_details, exc_info=True)
                     run_summary.append({
                         "project_id": project_id,
                         "interconnect": ic.name,
                         "outage_id": "UNKNOWN",
                         "target_state": "UNKNOWN",
                         "current_state": "UNKNOWN",
                         "action": "ERROR",
                         "status": f"FAILED: {str(e)[:500]}"
                     })
                     failed_projects.add(project_id)
                      
        except gcp_exceptions.Forbidden as e:
            error_details = f"CRITICAL: IAM PERMISSION DENIED listing project '{project_id}'. The orchestrator Service Account is missing 'compute.interconnects.list'. Details: {e}"
            logging.error(error_details, exc_info=True)
            run_summary.append({
                "project_id": project_id,
                "interconnect": "ALL_LINKS",
                "outage_id": "N/A",
                "target_state": "N/A",
                "current_state": "N/A",
                "action": "ERROR",
                "status": "FAILED: IAM_PERMISSION_DENIED"
            })
            failed_projects.add(project_id)
        except Exception as e:
            error_details = f"CRITICAL: Maintenance events loop failure listing project '{project_id}': {str(e)}"
            logging.error(error_details, exc_info=True)
            run_summary.append({
                "project_id": project_id,
                "interconnect": "ALL_LINKS",
                "outage_id": "N/A",
                "target_state": "N/A",
                "current_state": "N/A",
                "action": "ERROR",
                "status": f"FAILED: {str(e)[:500]}"
            })
            failed_projects.add(project_id)
            
    return run_summary, routers_to_align, failed_projects

def _evaluate_and_consolidate(run_summary: list, routers_to_align: dict) -> dict:
    """Evaluates target states and consolidates BGP peer targets, resolving conflicts.
    
    Modifies run_summary in place by updating 'action' and 'status' fields,
    and removing temporary '_peer_targets' fields.
    
    Args:
        run_summary: List of Interconnect run summary records.
        routers_to_align: Ledger of consolidated target states.
        
    Returns:
        dict: Filtered ledger containing only routers that actually need patching.
    """
    filtered_routers_to_align = {}
    for rkey, mods_dict in routers_to_align.items():
         needs_alignment = False
         for peer_name, target in mods_dict.items():
              if not is_peer_aligned(target["target_policy_state"], target["is_drained_currently"]):
                   needs_alignment = True
                   break
         if needs_alignment:
              filtered_routers_to_align[rkey] = mods_dict

    for record in run_summary:
         if "_peer_targets" not in record:
              continue
         
         peer_targets = record["_peer_targets"]
         if not peer_targets:
              continue
         
         has_delta = False
         ic_target_state = record["target_state"]
         
         for target in peer_targets:
              rkey = (target["project_id"], target["region"], target["router_name"])
              peer_name = target["peer_name"]
              
              consolidated_target = routers_to_align[rkey][peer_name]["target_policy_state"]
              is_drained = target["is_drained_currently"]
              
              if not is_peer_aligned(consolidated_target, is_drained):
                   has_delta = True
                   ic_target_state = consolidated_target
                   break
         
         if has_delta:
              record["action"] = "DRAINED" if ic_target_state == "DRAINED" else "RESTORED"
              record["status"] = "PENDING_ALIGNMENT"
         else:
              record["action"] = "NO_ACTION"
              record["status"] = "SUCCESS"
          
         del record["_peer_targets"]
         
    return filtered_routers_to_align

def _align_routers_parallel(filtered_routers_to_align: dict, failed_projects: set) -> dict:
    """Executes Cloud Router patching in parallel using ThreadPoolExecutor.
    
    Modifies failed_projects in place if patching fails.
    
    Args:
        filtered_routers_to_align: Ledger of routers to patch.
        failed_projects: Set of project IDs to update on failure.
        
    Returns:
        dict: Results of alignment mapping router_key -> {"success": bool, "error": str}
    """
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

def _update_final_statuses(run_summary: list, router_alignment_results: dict):
    """Updates Interconnect summary records status based on BGP alignment outcomes.
    
    Modifies run_summary in place.
    
    Args:
        run_summary: List of Interconnect run summary records.
        router_alignment_results: Results from the alignment phase.
    """
    for record in run_summary:
         if record["status"] == "PENDING_ALIGNMENT":
              associated_routers = record.get("associated_routers", [])
              failed_router = next((r for r in associated_routers if r in router_alignment_results and not router_alignment_results[r]["success"]), None)
              if failed_router:
                   err = router_alignment_results[failed_router]["error"]
                   record["status"] = f"FAILED: {err}"
                   record["action"] = "ERROR"
              else:
                   record["status"] = "SUCCESS"

def is_peer_aligned(target_state: str, is_drained: bool) -> bool:
    """Returns True if the BGP peer's current state matches the target state."""
    return (target_state == "DRAINED" and is_drained) or (target_state == "NORMAL" and not is_drained)

# --- UTILS & FORMATTERS ---

def parse_epoch_ms(ts_val) -> datetime:
    """Parses standard API timestamp formats (epoch milliseconds) using native parsing."""
    if not ts_val:
         return None
    try:
         return datetime.fromtimestamp(int(ts_val) / 1000.0, tz=timezone.utc)
    except (ValueError, TypeError) as e:
         raise ValueError(f"CRITICAL: Unable to parse maintenance timestamp '{ts_val}'. Schema may have changed: {e}")

def parse_attachment_url(url: str) -> (str, str, str):
    pattern = r"/projects/([^/]+)/regions/([^/]+)/interconnectAttachments/([^/]+)$"
    match = re.search(pattern, url)
    if not match:
        raise ValueError(f"Unrecognized Interconnect Attachment resource URI structure: {url}")
    return match.group(1), match.group(2), match.group(3)

def log_sre_summary_table(records: list):
    """Constructs and logs a beautiful Markdown table for SRE reviews inside Cloud Logging."""
    if not records:
        logging.info("\n### GCI MAINTENANCE EVENTS SUMMARY: ZERO INTERCONNECTS DETECTED")
        return

    lines = []
    lines.append("\n### GCI Routing Maintenance Events Summary Run")
    lines.append("| PROJECT ID | INTERCONNECT | ACTIVE OUTAGE | TARGET STATE | CURRENT STATE | ACTION TAKEN | RESULT STATUS |")
    lines.append("| :--- | :--- | :--- | :--- | :--- | :--- | :--- |")
    
    for r in records:
         row = "| {} | {} | {} | {} | {} | {} | {} |".format(
             r["project_id"],
             r["interconnect"],
             r["outage_id"],
             r["target_state"],
             r["current_state"],
             r["action"],
             r["status"]
         )
         lines.append(row)
         
    lines.append("\n")
    
    # Log the entire formatted table as a single text block
    logging.info("\n".join(lines))

# Execution entrypoint
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Drain/undrain Interconnect connections before/after maintenance events, by applying bgp routing policy on affected bgp sessions")
    parser.add_argument(
        "--projects",
        default="",
        help="Optional: Comma-separated list of GCP Project IDs to scan. If omitted, falls back to INTERCONNECT_PROJECTS env var."
    )
    args = parser.parse_args()

    process_maintenance_events(
        target_projects=args.projects
    )
