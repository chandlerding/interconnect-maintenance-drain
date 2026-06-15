import unittest
import os
from unittest.mock import MagicMock
from datetime import datetime, timezone, timedelta
from google.cloud import compute_v1
from src import main

# This test requires credentials and a real GCP environment to read from.
# It will NOT make any modifications (writes are mocked) unless --enable-writes is specified.
# Run via CLI parameters (Recommended):
#   python3 -m tests.live_simulation_test --project="your-project" --interconnect="your-ic"
# Or via environment variables (CI/CD Fallback):
#   LIVE_TEST_PROJECT="your-project" LIVE_TEST_INTERCONNECT="your-ic" python3 -m tests.live_simulation_test

class LiveMaintenanceSimulationTest(unittest.TestCase):
    target_project_id = None
    target_interconnect_name = None
    enable_real_writes = False
    enforce_no_op = False

    def setUp(self):
        self.project_id = self.target_project_id or os.environ.get("LIVE_TEST_PROJECT")
        self.target_ic_name = self.target_interconnect_name or os.environ.get("LIVE_TEST_INTERCONNECT")
        self.enable_writes = self.enable_real_writes or (os.environ.get("LIVE_TEST_ENABLE_WRITES") == "1")
        self.config = main.OrchestratorConfig()
        if self.enforce_no_op or (os.environ.get("NO_OP_POLICIES") == "1"):
            self.config.no_op_policies = True
        
        if not self.project_id or not self.target_ic_name:
            self.skipTest("Target project and interconnect link must be specified via CLI flags or environment variables to run live simulation.")

    def test_live_simulation(self):
        if self.enable_writes:
            self.run_live_simulation_with_writes()
        else:
            self.run_live_simulation_read_only()

    def run_live_simulation_read_only(self):
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
                if ic.name == self.target_ic_name:
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
        print(f"\n[Live Sim] Running orchestrator against real project '{self.project_id}'...")
        orchestrator = main.MaintenanceOrchestrator(
            config=self.config,
            interconnects_client=hybrid_ic_client,
            attachments_client=compute_v1.InterconnectAttachmentsClient,
            routers_client=routers_client_factory
        )
        orchestrator.process_maintenance_events(target_projects=self.project_id)

        self.assertTrue(target_found, f"Target Interconnect '{self.target_ic_name}' not found in project '{self.project_id}'")

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

    def run_live_simulation_with_writes(self):
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
                if ic.name == self.target_ic_name:
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

        associated_routers = self._get_associated_routers(real_ic_client)
        if not associated_routers:
             self.skipTest(f"No routers associated with interconnect {self.target_ic_name}. Cannot test writes.")
        print(f"[Live Sim] Associated routers for verification: {associated_routers}")

        # Run Phase 1: DRAIN
        print("\n[Live Sim] Phase 1: Draining (Applying policies)...")
        orchestrator = main.MaintenanceOrchestrator(
            config=self.config, interconnects_client=hybrid_ic_client
        )
        orchestrator.process_maintenance_events(target_projects=self.project_id)
            
        # Verify Phase 1
        print("\n[Live Sim] Verifying Phase 1 (Drain) on GCP...")
        for rkey in associated_routers:
            proj, region, router_name = rkey
            router = real_routers_client.get(project=proj, region=region, router=router_name)
            self._verify_router_state(router, expected_drained=True)

        # Run Phase 2: RESTORE
        print("\n[Live Sim] Phase 2: Restoring (Removing policies)...")
        inject_outage = False
        orchestrator.process_maintenance_events(target_projects=self.project_id)

        # Verify Phase 2
        print("\n[Live Sim] Verifying Phase 2 (Restore) on GCP...")
        for rkey in associated_routers:
            proj, region, router_name = rkey
            router = real_routers_client.get(project=proj, region=region, router=router_name)
            self._verify_router_state(router, expected_drained=False)
            
        print("\n[Live Sim] Real write verification completed successfully.")

    def _get_associated_routers(self, real_ic_client):
        routers = set()
        attachments_client = compute_v1.InterconnectAttachmentsClient()
        ics = real_ic_client.list(project=self.project_id)
        for ic in ics:
            if ic.name == self.target_ic_name:
                for attach_url in ic.interconnect_attachments:
                    proj, region, name = main.parse_attachment_url(attach_url)
                    attach_data = attachments_client.get(project=proj, region=region, interconnect_attachment=name)
                    router_url = attach_data.router
                    router_name = router_url.split("/")[-1]
                    routers.add((proj, region, router_name))
        return routers

    def _verify_router_state(self, router, expected_drained):
        real_ic_client = compute_v1.InterconnectsClient()
        target_attachments = set()
        ics = real_ic_client.list(project=self.project_id)
        for ic in ics:
            if ic.name == self.target_ic_name:
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
    parser.add_argument("--project", default="", help="Target GCP Project ID")
    parser.add_argument("--interconnect", default="", help="Target physical Dedicated Interconnect link name")
    parser.add_argument("--enable-writes", action="store_true", help="Enable actual policy creation and BGP peer session writes on live GCP resources")
    parser.add_argument("--no-op-policies", action="store_true", help="Deploy non-disruptive nextPolicy() BGP rules when real writes are enabled")
    args, remaining = parser.parse_known_args()
    
    if args.project:
        LiveMaintenanceSimulationTest.target_project_id = args.project
    if args.interconnect:
        LiveMaintenanceSimulationTest.target_interconnect_name = args.interconnect
    if args.enable_writes:
        LiveMaintenanceSimulationTest.enable_real_writes = args.enable_writes
    if args.no_op_policies:
        LiveMaintenanceSimulationTest.enforce_no_op = args.no_op_policies
        
    unittest.main(argv=[sys.argv[0]] + remaining)
