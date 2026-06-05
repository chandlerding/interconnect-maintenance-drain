import unittest
from unittest.mock import patch, MagicMock, call
from datetime import datetime, timezone, timedelta
from google.cloud import compute_v1
from src import main

class InterconnectMaintenanceIntegrationTest(unittest.TestCase):

    @patch('src.main.interconnects_client')
    @patch('src.main.attachments_client')
    @patch('src.main.get_routers_client') # Used in safely_patch_router
    @patch('src.main.routers_client')     # Used in check_current_bgp_states
    def test_maintenance_simulation(self, mock_routers_client_global, mock_get_routers_client, mock_attachments_client, mock_ic_client):
        """Simulates a maintenance event on one interconnect and verifies routing updates."""
        
        # 1. Setup Mock Topology & Resource States
        project_id = "test-project"
        region = "us-central1"
        now = datetime.now(timezone.utc)

        # --- Interconnects ---
        # IC 1: Under maintenance
        ic_m = MagicMock()
        ic_m.name = "ic-maintenance"
        outage = MagicMock()
        outage.name = "outage-1"
        outage.state = "ACTIVE"
        outage.start_time = int((now - timedelta(minutes=5)).timestamp() * 1000)
        outage.end_time = int((now + timedelta(minutes=55)).timestamp() * 1000)
        ic_m.expected_outages = [outage]
        ic_m.interconnect_attachments = [
            f"/projects/{project_id}/regions/{region}/interconnectAttachments/at-maintenance"
        ]

        # IC 2: Stable
        ic_s = MagicMock()
        ic_s.name = "ic-stable"
        ic_s.expected_outages = []
        ic_s.interconnect_attachments = [
            f"/projects/{project_id}/regions/{region}/interconnectAttachments/at-stable"
        ]

        mock_ic_client.list.return_value = [ic_m, ic_s]

        # --- VLAN Attachments ---
        at_m = MagicMock()
        at_m.name = "at-maintenance"
        at_m.router = f"/projects/{project_id}/regions/{region}/routers/router-maintenance"

        at_s = MagicMock()
        at_s.name = "at-stable"
        at_s.router = f"/projects/{project_id}/regions/{region}/routers/router-stable"

        def get_attachment(project, region, interconnect_attachment):
            if interconnect_attachment == "at-maintenance":
                return at_m
            elif interconnect_attachment == "at-stable":
                return at_s
            raise ValueError(f"Unknown attachment {interconnect_attachment}")
        mock_attachments_client.get.side_effect = get_attachment

        # --- Cloud Routers ---
        # Router for IC under maintenance
        r_m = MagicMock()
        r_m.name = "router-maintenance"
        r_m.bgp.asn = 64512
        
        if_m = MagicMock()
        if_m.name = "if-m"
        if_m.linked_interconnect_attachment = f".../interconnectAttachments/at-maintenance"
        r_m.interfaces = [if_m]
        
        peer_m = MagicMock()
        peer_m.name = "peer-m"
        peer_m.interface_name = "if-m"
        peer_m.import_policies = []
        peer_m.export_policies = []
        r_m.bgp_peers = [peer_m]

        # Router for stable IC
        r_s = MagicMock()
        r_s.name = "router-stable"
        r_s.bgp.asn = 64513
        
        if_s = MagicMock()
        if_s.name = "if-s"
        if_s.linked_interconnect_attachment = f".../interconnectAttachments/at-stable"
        r_s.interfaces = [if_s]
        
        peer_s = MagicMock()
        peer_s.name = "peer-s"
        peer_s.interface_name = "if-s"
        peer_s.import_policies = []
        peer_s.export_policies = []
        r_s.bgp_peers = [peer_s]

        # Mock Router Client (Global read-only client)
        def get_router_global(project, region, router):
            if router == "router-maintenance":
                return r_m
            elif router == "router-stable":
                return r_s
            raise ValueError(f"Unknown router {router}")
        mock_routers_client_global.get.side_effect = get_router_global
        mock_routers_client_global.list_route_policies.return_value = [] # Assume no policies exist yet

        # Mock Router Client (Thread-local write client)
        mock_routers_client_local = MagicMock()
        mock_get_routers_client.return_value = mock_routers_client_local
        mock_routers_client_local.get.side_effect = get_router_global
        mock_routers_client_local.list_route_policies.return_value = []
        
        # Mock operations
        mock_op = MagicMock()
        mock_routers_client_local.update_route_policy.return_value = mock_op
        mock_routers_client_local.patch.return_value = mock_op

        # 2. Run the Orchestrator
        main.process_maintenance_events(target_projects=project_id)

        # 3. Verifications
        
        # Verify that only router-maintenance was patched (since only ic-maintenance had outages)
        # safely_patch_router should have been called for router-maintenance
        mock_routers_client_local.patch.assert_called_once()
        args, kwargs = mock_routers_client_local.patch.call_args
        self.assertEqual(kwargs['router'], 'router-maintenance')
        
        # Verify that router-stable was NOT patched
        # We can check that patch was not called with router='router-stable'
        for call_args in mock_routers_client_local.patch.call_args_list:
            self.assertNotEqual(call_args[1].get('router'), 'router-stable')

        # Verify BGP policy injection on the patched router object passed to API
        patched_router = kwargs['router_resource']
        patched_peer = patched_router.bgp_peers[0]
        self.assertIn(main.config.import_policy_name, patched_peer.import_policies)
        self.assertIn(main.config.export_policy_name, patched_peer.export_policies)

        # Verify that policy creation was attempted for router-maintenance
        # should call update_route_policy twice (import and export)
        self.assertEqual(mock_routers_client_local.update_route_policy.call_count, 2)
        
        # Verify it was called for router-maintenance
        mock_routers_client_local.update_route_policy.assert_has_calls([
            call(project=project_id, region=region, router='router-maintenance', route_policy_resource=unittest.mock.ANY),
            call(project=project_id, region=region, router='router-maintenance', route_policy_resource=unittest.mock.ANY)
        ], any_order=True)

if __name__ == '__main__':
    unittest.main()
