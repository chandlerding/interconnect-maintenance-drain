import time
import random
import logging
import threading
import types
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from google.cloud import compute_v1
from google.api_core import exceptions as gcp_exceptions
from typing import List, Tuple, Callable, Any

try:
    from .config import OrchestratorConfig, BgpPeerTarget, InterconnectAuditResult, RouterReconciliationPlan
    from .utils import parse_epoch_ms, parse_attachment_url, is_peer_aligned, log_sre_summary_table
except ImportError:
    from config import OrchestratorConfig, BgpPeerTarget, InterconnectAuditResult, RouterReconciliationPlan
    from utils import parse_epoch_ms, parse_attachment_url, is_peer_aligned, log_sre_summary_table

class MaintenanceOrchestrator:
    """Main orchestrator class containing core alignment logic, decoupling dependencies and enabling scalable multi-threaded discovery."""
    def __init__(
        self, 
        config: OrchestratorConfig,
        interconnects_client = None,
        attachments_client = None,
        routers_client = None
    ):
        self.config = config
        
        def make_factory(client_arg, default_class):
            if client_arg is None:
                return default_class
            if isinstance(client_arg, (type, types.FunctionType)):
                return client_arg
            return lambda: client_arg
            
        self._ic_factory = make_factory(interconnects_client, compute_v1.InterconnectsClient)
        self._attach_factory = make_factory(attachments_client, compute_v1.InterconnectAttachmentsClient)
        self._router_factory = make_factory(routers_client, compute_v1.RoutersClient)
        
        self._thread_local = threading.local()

    @property
    def interconnects_client(self) -> compute_v1.InterconnectsClient:
        if not hasattr(self._thread_local, "ic_client"):
            self._thread_local.ic_client = self._ic_factory()
        return self._thread_local.ic_client

    @property
    def attachments_client(self) -> compute_v1.InterconnectAttachmentsClient:
        if not hasattr(self._thread_local, "attach_client"):
            self._thread_local.attach_client = self._attach_factory()
        return self._thread_local.attach_client

    @property
    def routers_client(self) -> compute_v1.RoutersClient:
        if not hasattr(self._thread_local, "router_client"):
            self._thread_local.router_client = self._router_factory()
        return self._thread_local.router_client

    def _execute_with_retry(self, fn: Callable, *args, **kwargs) -> Any:
        """Executes a Google Cloud API call with standardized exponential backoff and randomized jitter."""
        max_attempts = 5
        for attempt in range(max_attempts):
            try:
                return fn(*args, **kwargs)
            except (gcp_exceptions.TooManyRequests, gcp_exceptions.InternalServerError, gcp_exceptions.ServiceUnavailable, gcp_exceptions.PreconditionFailed) as e:
                if attempt >= max_attempts - 1:
                    raise RuntimeError(f"API operation exhausted after {max_attempts} retries. Final error: {e}") from e
                sleep_duration = (2 ** attempt) + random.uniform(0.1, 1.0)
                logging.warning(f"Transient GCP API concurrency error: {e}. Retrying in {sleep_duration:.2f}s (Attempt {attempt + 1}/{max_attempts})...")
                time.sleep(sleep_duration)

    def process_maintenance_events(self, target_projects: str = ""):
        logging.info(f"Maintenance Events Check Loop Started at {datetime.now(timezone.utc).isoformat()}")
        
        projects_to_use = target_projects if target_projects else self.config.projects
        if not projects_to_use:
            error_msg = "CRITICAL: Maintenance events loop aborted. No target projects specified (via argument or INTERCONNECT_PROJECTS env var)."
            logging.error(error_msg)
            raise ValueError(error_msg)

        project_list = list(dict.fromkeys(p.strip() for p in projects_to_use.split(",") if p.strip()))
        logging.info(f"Targeted networks maintenance events check scoped to project list: {project_list}")
        
        run_summary, failed_projects = self._audit_projects(project_list)
        reconciliation_plans = self._create_reconciliation_plans(run_summary)
        router_alignment_results = self._align_routers_parallel(reconciliation_plans, failed_projects)
        self._update_final_statuses(run_summary, router_alignment_results)
        
        log_sre_summary_table(run_summary)

        if failed_projects:
             raise RuntimeError(f"CRITICAL: Maintenance events completed with failures on projects: {list(failed_projects)}")

        logging.info("All target network project maintenance events checks completed successfully.")

    def process_interconnect_maintenance_events(
        self,
        project_id: str, 
        ic: compute_v1.Interconnect, 
        router_cache: dict
    ) -> Tuple[InterconnectAuditResult, List[BgpPeerTarget]]:
        ic_name = ic.name
        logging.info(f"Auditing Interconnect: '{ic_name}' [Project: {project_id}]")

        outages = list(ic.expected_outages) if ic.expected_outages else []
        if outages:
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

             drain_start = start_time - timedelta(minutes=self.config.lead_time_minutes)
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

        peer_states = self.check_current_bgp_states(attachments, router_cache)
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

    def check_current_bgp_states(self, attachments: list, router_cache: dict) -> list:
        peer_states = []
        
        for attach_url in attachments:
            try:
                 proj, region, name = parse_attachment_url(attach_url)
                 attach_data = self._execute_with_retry(
                     self.attachments_client.get, project=proj, region=region, interconnect_attachment=name
                 )
                 router_url = attach_data.router
                 if not router_url:
                      logging.warning(f"VLAN attachment {name} has no associated Cloud Router. Skipping.")
                      continue
                 
                 router_name = router_url.split("/")[-1]
                 
                 cache_key = (proj, region, router_name)
                 if cache_key not in router_cache:
                      router = self._execute_with_retry(
                          self.routers_client.get, project=proj, region=region, router=router_name
                      )
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
                           is_import_drained = self.config.import_policy_name in peer.import_policies
                           is_export_drained = self.config.export_policy_name in peer.export_policies
                           
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

    def toggle_drain_policies(self, peer: compute_v1.RouterBgpPeer, enable_drain: bool):
        """Cleanly modifies BGP peer route policies to enable or disable the drain state."""
        original_import = list(peer.import_policies)
        original_export = list(peer.export_policies)
        
        if enable_drain:
            new_import = [self.config.import_policy_name] + [x for x in original_import if x != self.config.import_policy_name]
            new_export = [self.config.export_policy_name] + [x for x in original_export if x != self.config.export_policy_name]
        else:
            new_import = [x for x in original_import if x != self.config.import_policy_name]
            new_export = [x for x in original_export if x != self.config.export_policy_name]

        peer.import_policies.clear()
        peer.import_policies.extend(new_import)
        peer.export_policies.clear()
        peer.export_policies.extend(new_export)

    def safely_patch_router(self, router_key: tuple, peer_mods: List[BgpPeerTarget]):
        proj, region, router_name = router_key
        
        logging.info(f"Beginning thread-safe write alignment for Cloud Router '{router_name}' under Project '{proj}'")
        
        max_attempts = 5
        attempt = 0
        
        while attempt < max_attempts:
             try:
                  router = self._execute_with_retry(
                      self.routers_client.get, project=proj, region=region, router=router_name
                  )
                  policies_patched = self.ensure_drain_policies_exist(proj, region, router)
                  
                  if policies_patched:
                       router = self._execute_with_retry(
                           self.routers_client.get, project=proj, region=region, router=router_name
                       )
                  
                  for mod in peer_mods:
                       for peer in router.bgp_peers:
                            if peer.name == mod.peer_name:
                                 if mod.target_policy_state == "DRAINED":
                                      self.toggle_drain_policies(peer, enable_drain=True)
                                      logging.info(f"Thread injecting Drain Route Policies into BGP peer '{mod.peer_name}' on Router '{router_name}'")
                                 else:
                                      self.toggle_drain_policies(peer, enable_drain=False)
                                      logging.info(f"Thread stripping Drain Route Policies from BGP peer '{mod.peer_name}' on Router '{router_name}'")

                  operation = self._execute_with_retry(
                      self.routers_client.patch,
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
                  sleep_duration = (2 ** attempt) + random.uniform(0.1, 1.0)
                  logging.warning(f"Transient write error on attempt {attempt}: {e}. Retrying in {sleep_duration:.2f}s...")
                  if attempt >= max_attempts:
                       raise RuntimeError(f"Unable to reconcile patch updates on Router {router_name} after {max_attempts} attempts. Error: {e}")
                  time.sleep(sleep_duration)
             except gcp_exceptions.Forbidden as e:
                  raise RuntimeError(f"CRITICAL: IAM PERMISSION DENIED patching Cloud Router '{router_name}' [Project: {proj}]. Details: {e}") from e
             except Exception as e:
                  raise RuntimeError(f"CRITICAL: Non-retryable error applying patch to Router {router_name}: {e}")

    def _is_term_valid(self, term: compute_v1.RoutePolicyPolicyTerm, expected_actions: List[str]) -> bool:
        if term.priority != 1:
            return False
        if not term.match or term.match.expression != self.config.wildcard_match_expr:
            return False
        
        term_actions = [a.expression for a in term.actions]
        return term_actions == expected_actions

    def _upsert_policy(
        self,
        project_id: str, 
        region: str, 
        router_name: str, 
        name: str, 
        policy_type: str, 
        expected_actions: List[str], 
        existing_policy: compute_v1.RoutePolicy
    ) -> bool:
        action_exprs = [compute_v1.Expr(expression=act) for act in expected_actions]
        match_expr = compute_v1.Expr(expression=self.config.wildcard_match_expr)

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
                self._is_term_valid(existing_policy.terms[0], expected_actions)
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
                operation = self._execute_with_retry(
                    self.routers_client.update_route_policy,
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

    def ensure_drain_policies_exist(self, project_id: str, region: str, router: compute_v1.Router) -> bool:
        if self.config.no_op_policies:
            expected_import_actions = list(self.config.no_op_policy_actions)
            expected_export_actions = list(self.config.no_op_policy_actions)
        else:
            expected_import_actions = [action.format(asn=router.bgp.asn) for action in self.config.import_policy_actions]
            expected_export_actions = [action.format(asn=router.bgp.asn) for action in self.config.export_policy_actions]

        logging.info(f"Reconciling route policies for router '{router.name}'. Expected import actions: {expected_import_actions}, export actions: {expected_export_actions}")

        try:
            existing_policies = list(self._execute_with_retry(
                self.routers_client.list_route_policies, project=project_id, region=region, router=router.name
            ))
        except Exception as e:
            logging.error(f"Error listing route policies for router {router.name}: {e}")
            raise

        import_policy = next((p for p in existing_policies if p.name == self.config.import_policy_name), None)
        export_policy = next((p for p in existing_policies if p.name == self.config.export_policy_name), None)

        import_patched = self._upsert_policy(project_id, region, router.name, self.config.import_policy_name, "ROUTE_POLICY_TYPE_IMPORT", expected_import_actions, import_policy)
        export_patched = self._upsert_policy(project_id, region, router.name, self.config.export_policy_name, "ROUTE_POLICY_TYPE_EXPORT", expected_export_actions, export_policy)

        return import_patched or export_patched

    def _audit_projects(self, project_list: list) -> Tuple[List[InterconnectAuditResult], set]:
        """Audits targeted GCP projects with multi-threaded Interconnect discovery."""
        run_summary = []
        router_cache = {}
        failed_projects = set()

        for project_id in project_list:
            logging.info(f"Scanning physical Interconnect resources under Project '{project_id}'")
            try:
                interconnects = list(self._execute_with_retry(self.interconnects_client.list, project=project_id))
                logging.info(f"Discovered {len(interconnects)} physical links inside project '{project_id}'. Launching multi-threaded discovery audits (Max 10 workers)...")
                
                with ThreadPoolExecutor(max_workers=10) as executor:
                     future_to_ic = {
                         executor.submit(
                             self.process_interconnect_maintenance_events, project_id, ic, router_cache
                         ): ic for ic in interconnects
                     }
                     
                     for future in as_completed(future_to_ic):
                         ic_obj = future_to_ic[future]
                         try:
                             record, peer_targets = future.result()
                             record._peer_targets = peer_targets
                             run_summary.append(record)
                         except gcp_exceptions.Forbidden as e:
                             error_details = f"CRITICAL: IAM PERMISSION DENIED processing physical link '{ic_obj.name}' under project '{project_id}'. Details: {e}"
                             logging.error(error_details, exc_info=True)
                             run_summary.append(InterconnectAuditResult(
                                 project_id=project_id, interconnect=ic_obj.name, outage_id="UNKNOWN",
                                 target_state="UNKNOWN", current_state="UNKNOWN", action="ERROR", status="FAILED: IAM_PERMISSION_DENIED"
                             ))
                             failed_projects.add(project_id)
                         except Exception as e:
                             error_details = f"CRITICAL: Maintenance events processing failure on link '{ic_obj.name}' under project '{project_id}': {str(e)}"
                             logging.error(error_details, exc_info=True)
                             run_summary.append(InterconnectAuditResult(
                                 project_id=project_id, interconnect=ic_obj.name, outage_id="UNKNOWN",
                                 target_state="UNKNOWN", current_state="UNKNOWN", action="ERROR", status=f"FAILED: {str(e)[:500]}"
                             ))
                             failed_projects.add(project_id)
                          
            except gcp_exceptions.Forbidden as e:
                error_details = f"CRITICAL: IAM PERMISSION DENIED listing project '{project_id}'. Details: {e}"
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
                
        return run_summary, failed_projects

    def _create_reconciliation_plans(self, run_summary: List[InterconnectAuditResult]) -> List[RouterReconciliationPlan]:
        """Separation of concerns: consolidates desired BGP peer states and produces executable reconciliation plans."""
        unified_peer_map = {} 
        
        for record in run_summary:
            if not record._peer_targets:
                continue
            
            for target in record._peer_targets:
                pkey = (target.project_id, target.region, target.router_name, target.peer_name)
                if pkey not in unified_peer_map:
                    unified_peer_map[pkey] = target
                else:
                    existing = unified_peer_map[pkey]
                    if target.target_policy_state == "DRAINED":
                        existing.target_policy_state = "DRAINED"

        router_plan_map = {} 
        for pkey, target in unified_peer_map.items():
            rkey = pkey[:3]
            if rkey not in router_plan_map:
                router_plan_map[rkey] = []
            router_plan_map[rkey].append(target)

        executable_plans = []
        for rkey, peer_targets in router_plan_map.items():
            needs_alignment = any(
                not is_peer_aligned(t.target_policy_state, t.is_drained_currently) 
                for t in peer_targets
            )
            if needs_alignment:
                proj, region, router_name = rkey
                executable_plans.append(RouterReconciliationPlan(
                    project_id=proj,
                    region=region,
                    router_name=router_name,
                    peer_targets=peer_targets
                ))

        for record in run_summary:
            if not record._peer_targets:
                continue
            
            has_delta = False
            ic_target_state = record.target_state
            
            for target in record._peer_targets:
                pkey = (target.project_id, target.region, target.router_name, target.peer_name)
                final_target_state = unified_peer_map[pkey].target_policy_state
                
                if not is_peer_aligned(final_target_state, target.is_drained_currently):
                    has_delta = True
                    ic_target_state = final_target_state
                    break
                    
            if has_delta:
                record.action = "DRAINED" if ic_target_state == "DRAINED" else "RESTORED"
                record.status = "PENDING_ALIGNMENT"
            else:
                record.action = "NO_ACTION"
                record.status = "SUCCESS"
                
            record.associated_routers = list(set((t.project_id, t.region, t.router_name) for t in record._peer_targets))
            record._peer_targets = []

        return executable_plans

    def _align_routers_parallel(self, plans: List[RouterReconciliationPlan], failed_projects: set) -> dict:
        router_alignment_results = {}
        write_futures = {}
        
        if plans:
             logging.info(f"Unified Router Ledger prepared with {len(plans)} distinct Cloud Routers to align.")
             
             with ThreadPoolExecutor(max_workers=4) as write_executor:
                  for plan in plans:
                       rkey = (plan.project_id, plan.region, plan.router_name)
                       future = write_executor.submit(self.safely_patch_router, rkey, plan.peer_targets)
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

    def _update_final_statuses(self, run_summary: List[InterconnectAuditResult], router_alignment_results: dict):
        for record in run_summary:
             if record.status == "PENDING_ALIGNMENT":
                  failed_router = next((r for r in record.associated_routers if r in router_alignment_results and not router_alignment_results[r]["success"]), None)
                  if failed_router:
                       err = router_alignment_results[failed_router]["error"]
                       record.status = f"FAILED: {err}"
                       record.action = "ERROR"
                  else:
                       record.status = "SUCCESS"

    def _safely_cleanup_router_policies(self, router_key: tuple):
        proj, region, router_name = router_key
        logging.info(f"Beginning thread-safe Route Policy cleanup for Cloud Router '{router_name}' under Project '{proj}'")
        
        router = self._execute_with_retry(
            self.routers_client.get, project=proj, region=region, router=router_name
        )
        
        peer_modified = False
        for peer in router.bgp_peers:
            if self.config.import_policy_name in peer.import_policies:
                peer.import_policies.remove(self.config.import_policy_name)
                peer_modified = True
                logging.info(f"Thread stripping Import Route Policy '{self.config.import_policy_name}' from BGP peer '{peer.name}' on Router '{router_name}'")
            if self.config.export_policy_name in peer.export_policies:
                peer.export_policies.remove(self.config.export_policy_name)
                peer_modified = True
                logging.info(f"Thread stripping Export Route Policy '{self.config.export_policy_name}' from BGP peer '{peer.name}' on Router '{router_name}'")
                
        if peer_modified:
             operation = self._execute_with_retry(
                 self.routers_client.patch,
                 project=proj,
                 region=region,
                 router=router_name,
                 router_resource=router
             )
             logging.info(f"PATCH operation '{operation.name}' dispatched to strip policies from BGP peers on Router '{router_name}'. Waiting...")
             operation.result()
             logging.info(f"PATCH operation '{operation.name}' completed successfully.")

        existing_policies = list(self._execute_with_retry(
            self.routers_client.list_route_policies, project=proj, region=region, router=router_name
        ))
        
        for policy_name in [self.config.import_policy_name, self.config.export_policy_name]:
            if any(p.name == policy_name for p in existing_policies):
                req = compute_v1.DeleteRoutePolicyRouterRequest(
                    project=proj, region=region, router=router_name, policy=policy_name
                )
                operation = self._execute_with_retry(
                    self.routers_client.delete_route_policy, request=req
                )
                logging.info(f"DELETE route policy '{policy_name}' operation '{operation.name}' dispatched on Router '{router_name}'. Waiting...")
                operation.result()
                logging.info(f"DELETE route policy '{policy_name}' completed successfully on Router '{router_name}'.")

    def cleanup_route_policies(self, target_projects: str = "") -> dict:
        logging.info(f"Automated BGP Route Policy Cleanup Loop Started at {datetime.now(timezone.utc).isoformat()}")
        
        projects_to_use = target_projects if target_projects else self.config.projects
        if not projects_to_use:
            raise ValueError("No target projects specified for policy cleanup.")

        project_list = list(dict.fromkeys(p.strip() for p in projects_to_use.split(",") if p.strip()))
        logging.info(f"Targeted policy cleanup scoped to project list: {project_list}")
        
        run_summary, failed_projects = self._audit_projects(project_list)
        
        routers_to_cleanup = set()
        for record in run_summary:
             for rkey in record.associated_routers:
                  routers_to_cleanup.add(rkey)
                  
        cleanup_results = {}
        if routers_to_cleanup:
             logging.info(f"Discovered {len(routers_to_cleanup)} distinct Cloud Routers to clean up.")
             with ThreadPoolExecutor(max_workers=4) as executor:
                  future_to_rkey = {
                      executor.submit(self._safely_cleanup_router_policies, rkey): rkey 
                      for rkey in routers_to_cleanup
                  }
                  for future in as_completed(future_to_rkey):
                      rkey = future_to_rkey[future]
                      proj, region, router_name = rkey
                      try:
                          future.result()
                          cleanup_results[rkey] = {"success": True, "error": None}
                          logging.info(f"Successfully completely cleaned up Route Policies on Router '{router_name}' [Project: {proj}]")
                      except Exception as e:
                          cleanup_results[rkey] = {"success": False, "error": str(e)}
                          logging.error(f"CRITICAL: Failed cleaning up Route Policies on Router '{router_name}' [Project: {proj}]: {e}", exc_info=True)
                          
        logging.info("All target network project route policy cleanup tasks completed.")
        return cleanup_results

    def manual_override_interconnect(self, target_ic_name: str, enforce_drain: bool, target_projects: str = ""):
        ic_list = list(dict.fromkeys(i.strip() for i in target_ic_name.split(",") if i.strip()))
        if len(ic_list) > 1:
            for ic in ic_list:
                self.manual_override_interconnect(ic, enforce_drain, target_projects)
            return

        target_state = "DRAINED" if enforce_drain else "NORMAL"
        override_type = "MANUAL_DRAIN" if enforce_drain else "MANUAL_UNDRAIN"
        logging.info(f"Manual Interconnect Override ({override_type}) initiated for link '{target_ic_name}' at {datetime.now(timezone.utc).isoformat()}")
        
        # Parse potential project-scoped syntax: 'projects/proj/global/interconnects/name' or 'proj/name'
        explicit_proj = None
        clean_ic_name = target_ic_name
        
        if "/interconnects/" in target_ic_name:
             parts = target_ic_name.split("/")
             try:
                 p_idx = parts.index("projects")
                 explicit_proj = parts[p_idx + 1]
                 clean_ic_name = parts[-1]
             except (ValueError, IndexError):
                 pass
        elif "/" in target_ic_name:
             parts = target_ic_name.split("/")
             if len(parts) == 2:
                  explicit_proj, clean_ic_name = parts[0], parts[1]

        scoped_projs = target_projects if target_projects else self.config.projects
        projects_to_use = explicit_proj if explicit_proj else scoped_projs
        if not projects_to_use:
            raise ValueError("No target projects specified to locate the target interconnect.")

        project_list = list(dict.fromkeys(p.strip() for p in projects_to_use.split(",") if p.strip()))
        logging.info(f"Scanning project list {project_list} to locate Target Interconnect '{clean_ic_name}'...")
        
        matched_ics = []
        router_cache = {}
        
        for proj in project_list:
            try:
                ics = list(self._execute_with_retry(self.interconnects_client.list, project=proj))
                for ic in ics:
                    if ic.name == clean_ic_name:
                        matched_ics.append((proj, ic))
                        break
            except gcp_exceptions.Forbidden:
                logging.warning(f"IAM Permission Denied inspecting project '{proj}'. Skipping.")
                
        if not matched_ics:
            error_msg = f"CRITICAL: Target Interconnect '{clean_ic_name}' not found across any scanned projects {project_list}."
            logging.error(error_msg)
            raise ValueError(error_msg)
            
        if len(matched_ics) > 1:
            ambiguous_projs = [p for p, _ in matched_ics]
            error_msg = f"Ambiguous interconnect name '{clean_ic_name}'. Found in multiple distinct projects: {ambiguous_projs}. To prevent unexpected cross-project routing alterations, please specify explicitly using 'project_id/interconnect_name' syntax."
            logging.error(error_msg)
            raise ValueError(error_msg)
            
        target_proj, target_ic = matched_ics[0]
        
        logging.info(f"Successfully located Target Interconnect '{clean_ic_name}' inside Project '{target_proj}'. Identifying attached VLAN circuits...")
        
        attachments = list(target_ic.interconnect_attachments) if target_ic.interconnect_attachments else []
        if not attachments:
             logging.info(f"No VLAN attachments mapped to Interconnect '{target_ic_name}'. Zero routing changes required.")
             record = InterconnectAuditResult(
                 project_id=target_proj,
                 interconnect=target_ic_name,
                 outage_id=override_type,
                 target_state=target_state,
                 current_state="N/A",
                 action="NO_ACTION",
                 status="SUCCESS: NO_ATTACHMENTS"
             )
             log_sre_summary_table([record])
             return

        peer_states = self.check_current_bgp_states(attachments, router_cache)
        current_state = "DRAINED" if any(p["is_drained"] for p in peer_states) else "NORMAL"
        associated_routers = list(set((p["project_id"], p["region"], p["router_name"]) for p in peer_states))
        
        logging.info(f"Manual Override on '{target_ic_name}' [Project: {target_proj}] -> TargetState: {target_state}, CurrentState: {current_state}")

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

        record = InterconnectAuditResult(
            project_id=target_proj,
            interconnect=target_ic_name,
            outage_id=override_type,
            target_state=target_state,
            current_state=current_state,
            action="NO_ACTION",
            status="SUCCESS",
            associated_routers=associated_routers,
            _peer_targets=peer_targets
        )

        failed_projects = set()
        reconciliation_plans = self._create_reconciliation_plans([record])
        router_alignment_results = self._align_routers_parallel(reconciliation_plans, failed_projects)
        self._update_final_statuses([record], router_alignment_results)
        
        log_sre_summary_table([record])

        if failed_projects:
             raise RuntimeError(f"CRITICAL: Manual override completed with failures on projects: {list(failed_projects)}")
             
        logging.info(f"Manual interconnect override ({override_type}) successfully applied to '{target_ic_name}'.")
