import unittest
from unittest.mock import MagicMock
from datetime import datetime, timezone, timedelta
from google.cloud import compute_v1
from src import main

class InterconnectMaintenanceIntegrationTest(unittest.TestCase):

    def test_maintenance_simulation(self):
        """Simulates maintenance events across multiple interconnects and projects using MaintenanceOrchestrator dependency injection."""
        
        # 1. Setup Concrete Compute Models
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

        mock_ic_client = MagicMock()
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

        mock_attachments_client = MagicMock()
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
                    import_policies=[main.OrchestratorConfig().import_policy_name] if is_drained else [],
                    export_policies=[main.OrchestratorConfig().export_policy_name] if is_drained else []
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

        mock_routers_client = MagicMock()
        def get_router_global(project, region, router):
            if router in routers:
                return routers[router]
            raise ValueError(f"Unknown router {router}")
        
        mock_routers_client.get.side_effect = get_router_global
        mock_routers_client.list_route_policies.return_value = []
        
        mock_op = MagicMock()
        mock_routers_client.update_route_policy.return_value = mock_op
        mock_routers_client.patch.return_value = mock_op

        # 2. Run the Orchestrator via Dependency Injection
        config = main.OrchestratorConfig()
        orchestrator = main.MaintenanceOrchestrator(
            config=config,
            interconnects_client=mock_ic_client,
            attachments_client=mock_attachments_client,
            routers_client=mock_routers_client
        )
        orchestrator.process_maintenance_events(target_projects=f"{project_id},{project_id_2}")

        # 3. Verifications
        patched_routers = [call_args[1].get('router') for call_args in mock_routers_client.patch.call_args_list]

        # Verify patched routers
        self.assertIn('router-maintenance', patched_routers)
        self.assertIn('router-recovered', patched_routers)
        self.assertIn('router-cross-project', patched_routers)

        # Verify ignored routers
        self.assertNotIn('router-stable', patched_routers)
        self.assertNotIn('router-no-bgp', patched_routers)
        
        # Verify the nature of the patches
        for call_args in mock_routers_client.patch.call_args_list:
            router_name = call_args[1].get('router')
            patched_router_obj = call_args[1].get('router_resource')
            patched_peer = patched_router_obj.bgp_peers[0]
            
            if router_name in ['router-maintenance', 'router-cross-project']:
                self.assertIn(config.import_policy_name, patched_peer.import_policies)
                self.assertIn(config.export_policy_name, patched_peer.export_policies)
            elif router_name == 'router-recovered':
                self.assertNotIn(config.import_policy_name, patched_peer.import_policies)
                self.assertNotIn(config.export_policy_name, patched_peer.export_policies)

        # Verify route policy creations
        policy_creations = [call_args[1].get('router') for call_args in mock_routers_client.update_route_policy.call_args_list]
        self.assertEqual(policy_creations.count('router-maintenance'), 2)
        self.assertEqual(policy_creations.count('router-cross-project'), 2)
        self.assertEqual(policy_creations.count('router-recovered'), 2)

if __name__ == '__main__':
    unittest.main()
