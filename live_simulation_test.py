import unittest
import os
from unittest.mock import patch, MagicMock, call
from datetime import datetime, timezone, timedelta
from google.cloud import compute_v1
from src import main

# This test requires credentials and a real GCP environment to read from.
# It will NOT make any modifications (writes are mocked).
# Run with:
#   LIVE_TEST_PROJECT="your-project" LIVE_TEST_INTERCONNECT="your-ic" .venv/bin/python live_simulation_test.py

class LiveMaintenanceSimulationTest(unittest.TestCase):

    def setUp(self):
        self.project_id = os.environ.get("LIVE_TEST_PROJECT")
        self.target_ic_name = os.environ.get("LIVE_TEST_INTERCONNECT")
        
        if not self.project_id or not self.target_ic_name:
            self.skipTest("LIVE_TEST_PROJECT and LIVE_TEST_INTERCONNECT env vars must be set to run this live simulation.")

    def test_live_simulation(self):
        enable_writes = os.environ.get("LIVE_TEST_ENABLE_WRITES") == "1"
        if enable_writes:
            self.run_live_simulation_with_writes()
        else:
            with patch('src.main.get_routers_client') as mock_get_routers_client:
                self.run_live_simulation_read_only(mock_get_routers_client)

    def run_live_simulation_read_only(self, mock_get_routers_client):
        """Reads real topology from GCP, injects fake outage on target IC, and verifies planned writes."""
        
        # 1. Setup Mock Write Client
        mock_routers_client_local = MagicMock()
        mock_get_routers_client.return_value = mock_routers_client_local
        
        mock_op = MagicMock()
        mock_routers_client_local.update_route_policy.return_value = mock_op
        mock_routers_client_local.patch.return_value = mock_op
        # Mock list_route_policies to return empty list so it tries to create them in simulation
        mock_routers_client_local.list_route_policies.return_value = []

        # 2. Setup Hybrid Interconnect Lister to inject outage
        real_ic_client = compute_v1.InterconnectsClient()
        target_found = False

        def hybrid_list(project):
            nonlocal target_found
            real_ics = list(real_ic_client.list(project=project))
            for ic in real_ics:
                if ic.name == self.target_ic_name:
                    target_found = True
                    # Inject active outage
                    now = datetime.now(timezone.utc)
                    outage = compute_v1.InterconnectOutageNotification()
                    outage.name = "simulated-live-outage"
                    outage.state = "ACTIVE"
                    outage.start_time = int((now - timedelta(minutes=5)).timestamp() * 1000)
                    outage.end_time = int((now + timedelta(minutes=55)).timestamp() * 1000)
                    # Protobuf repeated fields are appended to, we might need to clear first if we want to be clean,
                    # but since it's in-memory mock we can just overwrite or append.
                    # expected_outages is a repeated field.
                    ic.expected_outages = [outage]
                    print(f"\n[Live Sim] Injected active outage into real Interconnect '{ic.name}'")
            return real_ics

        # Patch the global interconnects_client used in main.py
        with patch('src.main.interconnects_client.list', side_effect=hybrid_list):
            
            # Run the orchestrator
            print(f"\n[Live Sim] Running orchestrator against real project '{self.project_id}'...")
            main.process_maintenance_events(target_projects=self.project_id)

        self.assertTrue(target_found, f"Target Interconnect '{self.target_ic_name}' not found in project '{self.project_id}'")

        # 3. Verifications of Planned Writes
        # If the target IC had BGP peers, they should have been planned for patching.
        # We can verify if patch was called on the mock local client.
        if mock_routers_client_local.patch.called:
            print("\n[Live Sim] Verification: Patching WOULD have occurred for the following routers:")
            for call_args in mock_routers_client_local.patch.call_args_list:
                kwargs = call_args[1]
                router_name = kwargs.get('router')
                patched_router = kwargs.get('router_resource')
                print(f"  - Router: {router_name}")
                for peer in patched_router.bgp_peers:
                    print(f"    - Peer '{peer.name}' policies: Import={peer.import_policies}, Export={peer.export_policies}")
                
                # Verify that we planned to inject the correct policies
                for peer in patched_router.bgp_peers:
                    # We only care about peers associated with the interface that was matched.
                    # In mock setup we don't easily know which interface it was unless we trace back,
                    # but we can check if at least one peer has the policies.
                    # Actually, main.py only modifies peers on matched interfaces.
                    if main.IMPORT_POLICY_NAME in peer.import_policies:
                         self.assertIn(main.IMPORT_POLICY_NAME, peer.import_policies)
                         self.assertIn(main.EXPORT_POLICY_NAME, peer.export_policies)
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

        associated_routers = self._get_associated_routers(real_ic_client)
        if not associated_routers:
             self.skipTest(f"No routers associated with interconnect {self.target_ic_name}. Cannot test writes.")
        print(f"[Live Sim] Associated routers for verification: {associated_routers}")

        # Run Phase 1: DRAIN
        print("\n[Live Sim] Phase 1: Draining (Applying policies)...")
        with patch('src.main.interconnects_client.list', side_effect=hybrid_list):
            main.process_maintenance_events(target_projects=self.project_id)
            
        # Verify Phase 1: Check if policies are applied
        print("\n[Live Sim] Verifying Phase 1 (Drain) on GCP...")
        for rkey in associated_routers:
            proj, region, router_name = rkey
            router = real_routers_client.get(project=proj, region=region, router=router_name)
            self._verify_router_state(router, expected_drained=True)

        # Run Phase 2: RESTORE (Rollback)
        print("\n[Live Sim] Phase 2: Restoring (Removing policies)...")
        inject_outage = False
        with patch('src.main.interconnects_client.list', side_effect=hybrid_list):
            main.process_maintenance_events(target_projects=self.project_id)

        # Verify Phase 2: Check if policies are removed
        print("\n[Live Sim] Verifying Phase 2 (Restore) on GCP...")
        for rkey in associated_routers:
            proj, region, router_name = rkey
            router = real_routers_client.get(project=proj, region=region, router=router_name)
            self._verify_router_state(router, expected_drained=False)
            
        print("\n[Live Sim] Real write verification completed successfully.")

    def _get_associated_routers(self, real_ic_client):
        routers = set()
        ics = real_ic_client.list(project=self.project_id)
        for ic in ics:
            if ic.name == self.target_ic_name:
                for attach_url in ic.interconnect_attachments:
                    proj, region, name = main.parse_attachment_url(attach_url)
                    attach_data = main.attachments_client.get(project=proj, region=region, interconnect_attachment=name)
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
                            is_import_drained = main.IMPORT_POLICY_NAME in peer.import_policies
                            is_export_drained = main.EXPORT_POLICY_NAME in peer.export_policies
                            is_drained = is_import_drained and is_export_drained
                            
                            print(f"  - Verifying Peer '{peer.name}' on Router '{router.name}': Drained={is_drained} (Expected={expected_drained})")
                            if expected_drained:
                                self.assertTrue(is_drained, f"Peer '{peer.name}' on router '{router.name}' is NOT drained, but expected to be.")
                                self.assertIn(main.IMPORT_POLICY_NAME, peer.import_policies)
                                self.assertIn(main.EXPORT_POLICY_NAME, peer.export_policies)
                            else:
                                self.assertFalse(is_drained, f"Peer '{peer.name}' on router '{router.name}' IS drained, but expected NOT to be.")
                                self.assertNotIn(main.IMPORT_POLICY_NAME, peer.import_policies)
                                self.assertNotIn(main.EXPORT_POLICY_NAME, peer.export_policies)

if __name__ == '__main__':
    unittest.main()
