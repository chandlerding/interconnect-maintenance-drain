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
        """Simulates maintenance events across multiple interconnects and projects, verifying routing updates using concrete compute_v1 models."""
        
        # 1. Setup Mock Topology & Resource States
        project_id = "test-project"
        project_id_2 = "other-project"
        region = "us-central1"
        now = datetime.now(timezone.utc)

        def create_outage(state, minutes_offset_start, minutes_offset_end):
            return compute_v1.InterconnectOutageNotification(
                name=f"outage-{minutes_offset_start}",
                state=state,
                start_time=int((now + timedelta(minutes=minutes_offset_start)).timestamp() * 1000),
                end_time=int((now + timedelta(minutes=minutes_offset_end)).timestamp() * 1000)
            )

        # --- Interconnects ---
        ic_m = compute_v1.Interconnect(
            name="ic-maintenance",
            expected_outages=[create_outage("ACTIVE", -5, 55)],
            interconnect_attachments=[f"/projects/{project_id}/regions/{region}/interconnectAttachments/at-maintenance"]
        )

        ic_s = compute_v1.Interconnect(
            name="ic-stable",
            expected_outages=[],
            interconnect_attachments=[f"/projects/{project_id}/regions/{region}/interconnectAttachments/at-stable"]
        )

        ic_r = compute_v1.Interconnect(
            name="ic-recovered",
            expected_outages=[],
            interconnect_attachments=[f"/projects/{project_id}/regions/{region}/interconnectAttachments/at-recovered"]
        )

        ic_na = compute_v1.Interconnect(
            name="ic-no-attachments",
            expected_outages=[create_outage("ACTIVE", -5, 55)],
            interconnect_attachments=[]
        )

        ic_nbgp = compute_v1.Interconnect(
            name="ic-no-bgp",
            expected_outages=[create_outage("ACTIVE", -5, 55)],
            interconnect_attachments=[f"/projects/{project_id}/regions/{region}/interconnectAttachments/at-no-bgp"]
        )

        ic_cp = compute_v1.Interconnect(
            name="ic-cross-project",
            expected_outages=[create_outage("ACTIVE", -5, 55)],
            interconnect_attachments=[f"/projects/{project_id_2}/regions/{region}/interconnectAttachments/at-cross-project"]
        )

        def list_interconnects(project):
            if project == project_id:
                return [ic_m, ic_s, ic_r, ic_na, ic_nbgp]
            elif project == project_id_2:
                return [ic_cp]
            return []
        mock_ic_client.list.side_effect = list_interconnects

        # --- VLAN Attachments ---
        attachments = {
            "at-maintenance": f"/projects/{project_id}/regions/{region}/routers/router-maintenance",
            "at-stable": f"/projects/{project_id}/regions/{region}/routers/router-stable",
            "at-recovered": f"/projects/{project_id}/regions/{region}/routers/router-recovered",
            "at-no-bgp": f"/projects/{project_id}/regions/{region}/routers/router-no-bgp",
            "at-cross-project": f"/projects/{project_id_2}/regions/{region}/routers/router-cross-project",
        }

        def get_attachment(project, region, interconnect_attachment):
            if interconnect_attachment in attachments:
                return compute_v1.InterconnectAttachment(
                    name=interconnect_attachment,
                    router=attachments[interconnect_attachment]
                )
            raise ValueError(f"Unknown attachment {interconnect_attachment}")
        mock_attachments_client.get.side_effect = get_attachment

        # --- Cloud Routers ---
        def create_router_mock(name, asn, peer_name=None, is_drained=False):
            interfaces = []
            bgp_peers = []
            if peer_name:
                ifc = compute_v1.RouterInterface(
                    name=f"if-{name}",
                    linked_interconnect_attachment=f".../interconnectAttachments/at-{name.replace('router-', '')}"
                )
                interfaces.append(ifc)
                
                peer = compute_v1.RouterBgpPeer(
                    name=peer_name,
                    interface_name=ifc.name,
                    import_policies=[main.config.import_policy_name] if is_drained else [],
                    export_policies=[main.config.export_policy_name] if is_drained else []
                )
                bgp_peers.append(peer)
            
            return compute_v1.Router(
                name=name,
                bgp=compute_v1.RouterBgp(asn=asn),
                interfaces=interfaces,
                bgp_peers=bgp_peers
            )

        routers = {
            "router-maintenance": create_router_mock("router-maintenance", 64512, "peer-m"),
            "router-stable": create_router_mock("router-stable", 64513, "peer-s"),
            "router-recovered": create_router_mock("router-recovered", 64514, "peer-r", is_drained=True),
            "router-no-bgp": create_router_mock("router-no-bgp", 64515, None),
            "router-cross-project": create_router_mock("router-cross-project", 64516, "peer-cp"),
        }

        def get_router_global(project, region, router):
            if router in routers:
                return routers[router]
            raise ValueError(f"Unknown router {router}")
        
        mock_routers_client_global.get.side_effect = get_router_global
        mock_routers_client_global.list_route_policies.return_value = []

        mock_routers_client_local = MagicMock()
        mock_get_routers_client.return_value = mock_routers_client_local
        mock_routers_client_local.get.side_effect = get_router_global
        mock_routers_client_local.list_route_policies.return_value = []
        
        mock_op = MagicMock()
        mock_routers_client_local.update_route_policy.return_value = mock_op
        mock_routers_client_local.patch.return_value = mock_op

        # 2. Run the Orchestrator
        main.process_maintenance_events(target_projects=f"{project_id},{project_id_2}")

        # 3. Verifications
        patched_routers = [call_args[1].get('router') for call_args in mock_routers_client_local.patch.call_args_list]

        # Verify router-maintenance was patched (drain)
        self.assertIn('router-maintenance', patched_routers)
        
        # Verify router-recovered was patched (restore)
        self.assertIn('router-recovered', patched_routers)
        
        # Verify router-cross-project was patched (drain)
        self.assertIn('router-cross-project', patched_routers)

        # Verify ignored routers were NOT patched
        self.assertNotIn('router-stable', patched_routers)
        self.assertNotIn('router-no-bgp', patched_routers)
        
        # Verify the nature of the patches
        for call_args in mock_routers_client_local.patch.call_args_list:
            router_name = call_args[1].get('router')
            patched_router_obj = call_args[1].get('router_resource')
            patched_peer = patched_router_obj.bgp_peers[0]
            
            if router_name in ['router-maintenance', 'router-cross-project']:
                # Assert policies were added (drain)
                self.assertIn(main.config.import_policy_name, patched_peer.import_policies)
                self.assertIn(main.config.export_policy_name, patched_peer.export_policies)
            elif router_name == 'router-recovered':
                # Assert policies were removed (restore)
                self.assertNotIn(main.config.import_policy_name, patched_peer.import_policies)
                self.assertNotIn(main.config.export_policy_name, patched_peer.export_policies)

        # Verify route policy creations (2 for router-maintenance, 2 for router-cross-project, 2 for router-recovered)
        # ensure_drain_policies_exist runs unconditionally before patching to verify existence.
        policy_creations = [call_args[1].get('router') for call_args in mock_routers_client_local.update_route_policy.call_args_list]
        self.assertEqual(policy_creations.count('router-maintenance'), 2)
        self.assertEqual(policy_creations.count('router-cross-project'), 2)
        self.assertEqual(policy_creations.count('router-recovered'), 2)

if __name__ == '__main__':
    unittest.main()
