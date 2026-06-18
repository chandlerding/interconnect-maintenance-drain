import unittest
import os
import sys
from unittest.mock import MagicMock
from datetime import datetime, timezone, timedelta
from google.cloud import compute_v1
from src import main

# This test requires credentials and a real GCP environment to read from.
# It will NOT make any modifications (writes are mocked) unless --enable-writes is specified.

class LiveMaintenanceSimulationTest(unittest.TestCase):
    target_project_id = None
    target_interconnect_name = None
    enable_real_writes = False
    enforce_no_op = False
    target_lead_time_minutes = None

    def setUp(self):
        raw_proj_str = self.target_project_id or os.environ.get("LIVE_TEST_PROJECT", "")
        raw_ic_str = self.target_interconnect_name or os.environ.get("LIVE_TEST_INTERCONNECT", "")
        
        self.target_pairs = [] # List of (project_id, interconnect_name)
        
        if raw_ic_str:
             for item in raw_ic_str.split(","):
                  item = item.strip()
                  if not item:
                      continue
                  if "/" in item:
                      parts = item.split("/")
                      if len(parts) == 2:
                          self.target_pairs.append((parts[0].strip(), parts[1].strip()))
                  else:
                      projs = [p.strip() for p in raw_proj_str.split(",") if p.strip()]
                      if not projs and self.target_pairs:
                           projs = [self.target_pairs[0][0]]
                      for p in projs:
                           self.target_pairs.append((p, item))

        self.enable_writes = self.enable_real_writes or (os.environ.get("LIVE_TEST_ENABLE_WRITES") == "1")
        self.config = main.OrchestratorConfig()
        if self.enforce_no_op or (os.environ.get("NO_OP_POLICIES") == "1"):
            self.config.no_op_policies = True
        if self.target_lead_time_minutes is not None:
            self.config.lead_time_minutes = self.target_lead_time_minutes
            
        if not self.target_pairs:
            self.skipTest("Target project and interconnect link must be specified via CLI flags or environment variables to run live simulation.")

    def test_live_simulation(self):
        for target_proj, target_ic in self.target_pairs:
            print(f"\n==================================================================")
            print(f"[Live Sim Engine] Executing validation for '{target_proj}/{target_ic}'...")
            print(f"==================================================================")
            if self.enable_writes:
                self.run_live_simulation_with_writes(target_proj, target_ic)
            else:
                self.run_live_simulation_read_only(target_proj, target_ic)

    def run_live_simulation_read_only(self, target_proj, target_ic):
        """Reads real topology from GCP, injects fake outage on target IC, and verifies planned writes."""
        
        # 1. Setup Read-Only Routers Client Factory with Shared Mock Writes
        mock_op = MagicMock()
        shared_update_mock = MagicMock(return_value=mock_op)
        shared_patch_mock = MagicMock(return_value=mock_op)
        
        def routers_client_factory():
            client = compute_v1.RoutersClient()
            client.update_route_policy = shared_update_mock
            client.patch = shared_patch_mock
            client.list_route_policies = MagicMock(return_value=[])
            return client

        # 2. Setup Hybrid Interconnect Lister to inject outage
        real_ic_client = compute_v1.InterconnectsClient()
        target_found = False

        def hybrid_list(project):
            nonlocal target_found
            real_ics = list(real_ic_client.list(project=project))
            for ic in real_ics:
                if ic.name == target_ic:
                    target_found = True
                    now = datetime.now(timezone.utc)
                    outage = compute_v1.InterconnectOutageNotification()
                    outage.name = "simulated-live-outage"
                    outage.state = "ACTIVE"
                    outage.start_time = int((now - timedelta(minutes=5)).timestamp() * 1000)
                    outage.end_time = int((now + timedelta(minutes=55)).timestamp() * 1000)
                    ic.expected_outages = [outage]
                    print(f"\n[Live Sim] Injected active outage into real Interconnect '{ic.name}'")
            return real_ics

        hybrid_ic_client = MagicMock()
        hybrid_ic_client.list.side_effect = hybrid_list
            
        # Run the orchestrator via dependency injection
        print(f"\n[Live Sim] Running orchestrator against real project '{target_proj}'...")
        orchestrator = main.MaintenanceOrchestrator(
            config=self.config,
            interconnects_client=hybrid_ic_client,
            attachments_client=compute_v1.InterconnectAttachmentsClient,
            routers_client=routers_client_factory
        )
        orchestrator.process_maintenance_events(target_projects=target_proj)

        self.assertTrue(target_found, f"Target Interconnect '{target_ic}' not found in project '{target_proj}'")

        # 3. Verifications of Planned Writes
        if shared_patch_mock.called:
            print("\n[Live Sim] Verification: Patching WOULD have occurred for the following routers:")
            for call_args in shared_patch_mock.call_args_list:
                kwargs = call_args[1]
                router_name = kwargs.get('router')
                patched_router = kwargs.get('router_resource')
                print(f"  - Router: {router_name}")
                for peer in patched_router.bgp_peers:
                    print(f"    - Peer '{peer.name}' policies: Import={peer.import_policies}, Export={peer.export_policies}")
                
                for peer in patched_router.bgp_peers:
                    if self.config.import_policy_name in peer.import_policies:
                         self.assertIn(self.config.import_policy_name, peer.import_policies)
                         self.assertIn(self.config.export_policy_name, peer.export_policies)
        else:
            print("\n[Live Sim] Verification: No patching was planned. (Check if the target Interconnect has active VLAN attachments and BGP peers).")

    def run_live_simulation_with_writes(self, target_proj, target_ic):
        print("\n[Live Sim] RUNNING WITH REAL WRITES. WARNING: This will modify GCP resources.")
        
        real_ic_client = compute_v1.InterconnectsClient()
        real_routers_client = compute_v1.RoutersClient()
        target_found = False
        
        # 1. Setup Hybrid Interconnect Lister to inject outage (Drain Phase)
        inject_outage = True
        def hybrid_list(project):
            nonlocal target_found
            real_ics = list(real_ic_client.list(project=project))
            for ic in real_ics:
                if ic.name == target_ic:
                    target_found = True
                    if inject_outage:
                        now = datetime.now(timezone.utc)
                        outage = compute_v1.InterconnectOutageNotification()
                        outage.name = "simulated-live-outage"
                        outage.state = "ACTIVE"
                        outage.start_time = int((now - timedelta(minutes=5)).timestamp() * 1000)
                        outage.end_time = int((now + timedelta(minutes=55)).timestamp() * 1000)
                        ic.expected_outages = [outage]
                        print(f"\n[Live Sim] Injected active outage into real Interconnect '{ic.name}'")
            return real_ics

        hybrid_ic_client = MagicMock()
        hybrid_ic_client.list.side_effect = hybrid_list

        associated_routers = self._get_associated_routers(real_ic_client, target_proj, target_ic)
        if not associated_routers:
             print(f"No routers associated with interconnect {target_ic}. Cannot test writes.")
             return
        print(f"[Live Sim] Associated routers for verification: {associated_routers}")

        # Run Phase 1: DRAIN
        print("\n[Live Sim] Phase 1: Draining (Applying policies)...")
        orchestrator = main.MaintenanceOrchestrator(
            config=self.config, interconnects_client=hybrid_ic_client
        )
        orchestrator.process_maintenance_events(target_projects=target_proj)
            
        # Verify Phase 1
        print("\n[Live Sim] Verifying Phase 1 (Drain) on GCP...")
        for rkey in associated_routers:
            proj, region, router_name = rkey
            router = real_routers_client.get(project=proj, region=region, router=router_name)
            self._verify_router_state(router, True, target_proj, target_ic)

        # Run Phase 2: RESTORE
        print("\n[Live Sim] Phase 2: Restoring (Removing policies)...")
        inject_outage = False
        orchestrator.process_maintenance_events(target_projects=target_proj)

        # Verify Phase 2
        print("\n[Live Sim] Verifying Phase 2 (Restore) on GCP...")
        for rkey in associated_routers:
            proj, region, router_name = rkey
            router = real_routers_client.get(project=proj, region=region, router=router_name)
            self._verify_router_state(router, False, target_proj, target_ic)
            
        print("\n[Live Sim] Real write verification completed successfully.")

    def _get_associated_routers(self, real_ic_client, target_proj, target_ic):
        routers = set()
        attachments_client = compute_v1.InterconnectAttachmentsClient()
        ics = real_ic_client.list(project=target_proj)
        for ic in ics:
            if ic.name == target_ic:
                for attach_url in ic.interconnect_attachments:
                    proj, region, name = main.parse_attachment_url(attach_url)
                    attach_data = attachments_client.get(project=proj, region=region, interconnect_attachment=name)
                    router_url = attach_data.router
                    router_name = router_url.split("/")[-1]
                    routers.add((proj, region, router_name))
        return routers

    def _verify_router_state(self, router, expected_drained, target_proj, target_ic):
        real_ic_client = compute_v1.InterconnectsClient()
        target_attachments = set()
        ics = real_ic_client.list(project=target_proj)
        for ic in ics:
            if ic.name == target_ic:
                for attach_url in ic.interconnect_attachments:
                    target_attachments.add(attach_url.split("/")[-1])
                    
        for interface in router.interfaces:
            attachment_ref = getattr(interface, "linked_interconnect_attachment", "")
            if attachment_ref:
                attachment_name = attachment_ref.split("/")[-1]
                if attachment_name in target_attachments:
                    for peer in router.bgp_peers:
                        if peer.interface_name == interface.name:
                            is_import_drained = self.config.import_policy_name in peer.import_policies
                            is_export_drained = self.config.export_policy_name in peer.export_policies
                            is_drained = is_import_drained and is_export_drained
                            
                            print(f"  - Verifying Peer '{peer.name}' on Router '{router.name}': Drained={is_drained} (Expected={expected_drained})")
                            if expected_drained:
                                self.assertTrue(is_drained, f"Peer '{peer.name}' on router '{router.name}' is NOT drained, but expected to be.")
                                self.assertIn(self.config.import_policy_name, peer.import_policies)
                                self.assertIn(self.config.export_policy_name, peer.export_policies)
                            else:
                                self.assertFalse(is_drained, f"Peer '{peer.name}' on router '{router.name}' IS drained, but expected NOT to be.")
                                self.assertNotIn(self.config.import_policy_name, peer.import_policies)
                                self.assertNotIn(self.config.export_policy_name, peer.export_policies)

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description="Live GCP read-only simulation or write verification test")
    parser.add_argument("--projects", "--project", dest="project", default="", help="Target GCP Project ID(s)")
    parser.add_argument("--interconnect", "--interconnects", dest="interconnect", default="", help="Target physical Dedicated Interconnect link name(s)")
    parser.add_argument("--enable-writes", action="store_true", help="Enable actual policy creation and BGP peer session writes on live GCP resources")
    parser.add_argument("--no-op-policies", action="store_true", help="Deploy non-disruptive nextPolicy() BGP rules when real writes are enabled")
    parser.add_argument("--lead-time-minutes", type=int, default=None, help="Lead time in minutes before maintenance to start draining")
    args, remaining = parser.parse_known_args()
    
    if args.project:
        LiveMaintenanceSimulationTest.target_project_id = args.project
    if args.interconnect:
        LiveMaintenanceSimulationTest.target_interconnect_name = args.interconnect
    if args.enable_writes:
        LiveMaintenanceSimulationTest.enable_real_writes = args.enable_writes
    if args.no_op_policies:
        LiveMaintenanceSimulationTest.enforce_no_op = args.no_op_policies
    if args.lead_time_minutes is not None:
        LiveMaintenanceSimulationTest.target_lead_time_minutes = args.lead_time_minutes
        
    unittest.main(argv=[sys.argv[0]] + remaining)
