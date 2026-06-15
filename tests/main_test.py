import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock
from google.cloud import compute_v1
from src import main

class TestParseEpochMs(unittest.TestCase):

    def test_parse_epoch_milliseconds_int(self):
        ts = 1776735000000
        expected = datetime(2026, 4, 21, 1, 30, 0, tzinfo=timezone.utc)
        self.assertEqual(main.parse_epoch_ms(ts), expected)

    def test_parse_epoch_milliseconds_str(self):
        ts = "1776735000000"
        expected = datetime(2026, 4, 21, 1, 30, 0, tzinfo=timezone.utc)
        self.assertEqual(main.parse_epoch_ms(ts), expected)

    def test_parse_empty_value(self):
        self.assertIsNone(main.parse_epoch_ms(None))
        self.assertIsNone(main.parse_epoch_ms(""))

    def test_parse_invalid_format(self):
        with self.assertRaises(ValueError):
            main.parse_epoch_ms("invalid-date")

class TestIsPeerAligned(unittest.TestCase):
    def test_aligned_drained(self):
        self.assertTrue(main.is_peer_aligned("DRAINED", True))
    def test_aligned_normal(self):
        self.assertTrue(main.is_peer_aligned("NORMAL", False))
    def test_unaligned_drained(self):
        self.assertFalse(main.is_peer_aligned("DRAINED", False))
    def test_unaligned_normal(self):
        self.assertFalse(main.is_peer_aligned("NORMAL", True))

class TestUpdateFinalStatuses(unittest.TestCase):
    def setUp(self):
        self.orchestrator = main.MaintenanceOrchestrator(main.OrchestratorConfig())

    def test_update_success(self):
        run_summary = [
            main.InterconnectAuditResult(project_id="p1", interconnect="ic1", outage_id="NONE", target_state="DRAINED", current_state="NORMAL", action="NO_ACTION", status="PENDING_ALIGNMENT", associated_routers=[("p1", "r1", "rt1")])
        ]
        results = {("p1", "r1", "rt1"): {"success": True}}
        self.orchestrator._update_final_statuses(run_summary, results)
        self.assertEqual(run_summary[0].status, "SUCCESS")

    def test_update_failed(self):
        run_summary = [
            main.InterconnectAuditResult(project_id="p1", interconnect="ic1", outage_id="NONE", target_state="DRAINED", current_state="NORMAL", action="NO_ACTION", status="PENDING_ALIGNMENT", associated_routers=[("p1", "r1", "rt1")])
        ]
        results = {("p1", "r1", "rt1"): {"success": False, "error": "some error"}}
        self.orchestrator._update_final_statuses(run_summary, results)
        self.assertEqual(run_summary[0].status, "FAILED: some error")
        self.assertEqual(run_summary[0].action, "ERROR")

    def test_update_no_change_if_not_pending(self):
        run_summary = [
            main.InterconnectAuditResult(project_id="p1", interconnect="ic1", outage_id="NONE", target_state="NORMAL", current_state="NORMAL", action="NO_ACTION", status="SUCCESS: NO_ATTACHMENTS", associated_routers=[])
        ]
        results = {}
        self.orchestrator._update_final_statuses(run_summary, results)
        self.assertEqual(run_summary[0].status, "SUCCESS: NO_ATTACHMENTS")

class TestCreateReconciliationPlans(unittest.TestCase):
    def setUp(self):
        self.orchestrator = main.MaintenanceOrchestrator(main.OrchestratorConfig())

    def test_no_change_needed(self):
        run_summary = [
            main.InterconnectAuditResult(project_id="p1", interconnect="ic1", outage_id="NONE", target_state="NORMAL", current_state="NORMAL", action="NO_ACTION", status="SUCCESS", _peer_targets=[
                main.BgpPeerTarget(project_id="p1", region="r1", router_name="rt1", peer_name="gp1", target_policy_state="NORMAL", is_drained_currently=False)
            ])
        ]
        plans = self.orchestrator._create_reconciliation_plans(run_summary)
        self.assertEqual(len(plans), 0)
        self.assertEqual(run_summary[0].action, "NO_ACTION")
        self.assertEqual(run_summary[0].status, "SUCCESS")
        self.assertEqual(run_summary[0]._peer_targets, [])

    def test_alignment_needed_drain(self):
        run_summary = [
            main.InterconnectAuditResult(project_id="p1", interconnect="ic1", outage_id="NONE", target_state="DRAINED", current_state="NORMAL", action="NO_ACTION", status="SUCCESS", _peer_targets=[
                main.BgpPeerTarget(project_id="p1", region="r1", router_name="rt1", peer_name="gp1", target_policy_state="DRAINED", is_drained_currently=False)
            ])
        ]
        plans = self.orchestrator._create_reconciliation_plans(run_summary)
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0].router_name, "rt1")
        self.assertEqual(run_summary[0].action, "DRAINED")
        self.assertEqual(run_summary[0].status, "PENDING_ALIGNMENT")

    def test_alignment_needed_restore(self):
        run_summary = [
            main.InterconnectAuditResult(project_id="p1", interconnect="ic1", outage_id="NONE", target_state="NORMAL", current_state="NORMAL", action="NO_ACTION", status="SUCCESS", _peer_targets=[
                main.BgpPeerTarget(project_id="p1", region="r1", router_name="rt1", peer_name="gp1", target_policy_state="NORMAL", is_drained_currently=True)
            ])
        ]
        plans = self.orchestrator._create_reconciliation_plans(run_summary)
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0].router_name, "rt1")
        self.assertEqual(run_summary[0].action, "RESTORED")
        self.assertEqual(run_summary[0].status, "PENDING_ALIGNMENT")

class TestAuditProjects(unittest.TestCase):
    def setUp(self):
        self.mock_ic_client = MagicMock()
        self.orchestrator = main.MaintenanceOrchestrator(
            main.OrchestratorConfig(), interconnects_client=self.mock_ic_client
        )

    @patch.object(main.MaintenanceOrchestrator, 'process_interconnect_maintenance_events')
    def test_audit_success(self, mock_process):
        mock_ic = compute_v1.Interconnect(name="ic1")
        self.mock_ic_client.list.return_value = [mock_ic]

        mock_process.return_value = (
            main.InterconnectAuditResult(project_id="p1", interconnect="ic1", outage_id="NONE", target_state="DRAINED", current_state="NORMAL", action="NO_ACTION", status="SUCCESS"),
            [main.BgpPeerTarget(project_id="p1", region="r1", router_name="rt1", peer_name="gp1", target_policy_state="DRAINED", is_drained_currently=False)]
        )

        run_summary, failed_projects = self.orchestrator._audit_projects(["p1"])

        self.assertEqual(len(run_summary), 1)
        self.assertEqual(run_summary[0].interconnect, "ic1")
        self.assertEqual(len(failed_projects), 0)

    def test_audit_forbidden(self):
        from google.api_core import exceptions as gcp_exceptions
        self.mock_ic_client.list.side_effect = gcp_exceptions.Forbidden("Forbidden")

        run_summary, failed_projects = self.orchestrator._audit_projects(["p1"])

        self.assertEqual(len(run_summary), 1)
        self.assertEqual(run_summary[0].status, "FAILED: IAM_PERMISSION_DENIED")
        self.assertEqual(len(failed_projects), 1)
        self.assertIn("p1", failed_projects)

class TestProcessMaintenanceEvents(unittest.TestCase):
    def setUp(self):
        self.config = main.OrchestratorConfig()
        self.orchestrator = main.MaintenanceOrchestrator(self.config)

    @patch.object(main.MaintenanceOrchestrator, '_audit_projects')
    @patch.object(main.MaintenanceOrchestrator, '_create_reconciliation_plans')
    @patch.object(main.MaintenanceOrchestrator, '_align_routers_parallel')
    @patch.object(main.MaintenanceOrchestrator, '_update_final_statuses')
    @patch('src.orchestrator.log_sre_summary_table')
    def test_process_success(self, mock_log, mock_update, mock_align, mock_create, mock_audit):
        mock_audit.return_value = ([], set())
        mock_create.return_value = []
        mock_align.return_value = {}
        self.config.projects = "p1"

        self.orchestrator.process_maintenance_events()

        mock_audit.assert_called_once_with(["p1"])
        mock_create.assert_called_once()
        mock_align.assert_called_once()
        mock_update.assert_called_once()
        mock_log.assert_called_once()

    @patch.object(main.MaintenanceOrchestrator, '_audit_projects')
    def test_process_no_projects(self, mock_audit):
        self.config.projects = ""
        with self.assertRaises(ValueError):
            self.orchestrator.process_maintenance_events()

class TestProcessInterconnectMaintenanceEvents(unittest.TestCase):
    def setUp(self):
        self.orchestrator = main.MaintenanceOrchestrator(main.OrchestratorConfig())

    @patch.object(main.MaintenanceOrchestrator, 'check_current_bgp_states')
    def test_no_outages(self, mock_check_bgp):
        mock_ic = compute_v1.Interconnect(
            name="ic1",
            expected_outages=[],
            interconnect_attachments=["/projects/p1/regions/r1/interconnectAttachments/at1"]
        )

        mock_check_bgp.return_value = [
            {"project_id": "p1", "region": "r1", "router_name": "rt1", "peer_name": "gp1", "is_drained": False}
        ]

        record, peer_targets = self.orchestrator.process_interconnect_maintenance_events("p1", mock_ic, {})

        self.assertEqual(record.target_state, "NORMAL")
        self.assertEqual(record.current_state, "NORMAL")
        self.assertEqual(len(peer_targets), 1)
        self.assertEqual(peer_targets[0].target_policy_state, "NORMAL")

    @patch.object(main.MaintenanceOrchestrator, 'check_current_bgp_states')
    def test_active_outage(self, mock_check_bgp):
        now = datetime.now(timezone.utc)
        mock_outage = compute_v1.InterconnectOutageNotification(
            name="out1",
            state="ACTIVE",
            start_time=int((now - timedelta(minutes=10)).timestamp() * 1000),
            end_time=int((now + timedelta(minutes=50)).timestamp() * 1000)
        )
        mock_ic = compute_v1.Interconnect(
            name="ic1",
            expected_outages=[mock_outage],
            interconnect_attachments=["/projects/p1/regions/r1/interconnectAttachments/at1"]
        )

        mock_check_bgp.return_value = [
            {"project_id": "p1", "region": "r1", "router_name": "rt1", "peer_name": "gp1", "is_drained": False}
        ]

        record, peer_targets = self.orchestrator.process_interconnect_maintenance_events("p1", mock_ic, {})

        self.assertEqual(record.target_state, "DRAINED")
        self.assertEqual(record.current_state, "NORMAL")
        self.assertEqual(len(peer_targets), 1)
        self.assertEqual(peer_targets[0].target_policy_state, "DRAINED")

    @patch.object(main.MaintenanceOrchestrator, 'check_current_bgp_states')
    def test_imminent_outage(self, mock_check_bgp):
        now = datetime.now(timezone.utc)
        mock_outage = compute_v1.InterconnectOutageNotification(
            name="out1",
            state="ACTIVE",
            start_time=int((now + timedelta(minutes=30)).timestamp() * 1000),
            end_time=int((now + timedelta(minutes=90)).timestamp() * 1000)
        )
        mock_ic = compute_v1.Interconnect(
            name="ic1",
            expected_outages=[mock_outage],
            interconnect_attachments=["/projects/p1/regions/r1/interconnectAttachments/at1"]
        )

        mock_check_bgp.return_value = [
            {"project_id": "p1", "region": "r1", "router_name": "rt1", "peer_name": "gp1", "is_drained": False}
        ]

        record, peer_targets = self.orchestrator.process_interconnect_maintenance_events("p1", mock_ic, {})

        self.assertEqual(record.target_state, "DRAINED")

    @patch.object(main.MaintenanceOrchestrator, 'check_current_bgp_states')
    def test_future_outage_not_imminent(self, mock_check_bgp):
        now = datetime.now(timezone.utc)
        mock_outage = compute_v1.InterconnectOutageNotification(
            name="out1",
            state="ACTIVE",
            start_time=int((now + timedelta(minutes=120)).timestamp() * 1000),
            end_time=int((now + timedelta(minutes=180)).timestamp() * 1000)
        )
        mock_ic = compute_v1.Interconnect(
            name="ic1",
            expected_outages=[mock_outage],
            interconnect_attachments=["/projects/p1/regions/r1/interconnectAttachments/at1"]
        )

        mock_check_bgp.return_value = [
            {"project_id": "p1", "region": "r1", "router_name": "rt1", "peer_name": "gp1", "is_drained": False}
        ]

        record, peer_targets = self.orchestrator.process_interconnect_maintenance_events("p1", mock_ic, {})

        self.assertEqual(record.target_state, "NORMAL")

class TestRoutePolicyCleanup(unittest.TestCase):
    def setUp(self):
        self.config = main.OrchestratorConfig()
        self.mock_ic_client = MagicMock()
        self.mock_attach_client = MagicMock()
        self.mock_router_client = MagicMock()
        
        self.orchestrator = main.MaintenanceOrchestrator(
            config=self.config,
            interconnects_client=self.mock_ic_client,
            attachments_client=self.mock_attach_client,
            routers_client=self.mock_router_client
        )

    def test_cleanup_route_policies(self):
        mock_ic = compute_v1.Interconnect(
            name="ic-cleanup",
            interconnect_attachments=["/projects/p1/regions/r1/interconnectAttachments/at1"]
        )
        self.mock_ic_client.list.return_value = [mock_ic]
        
        mock_attach = compute_v1.InterconnectAttachment(
            name="at1",
            router="https://www.googleapis.com/compute/v1/projects/p1/regions/r1/routers/rt1"
        )
        self.mock_attach_client.get.return_value = mock_attach

        mock_interface = compute_v1.RouterInterface(
            name="if1",
            linked_interconnect_attachment="https://www.googleapis.com/compute/v1/projects/p1/regions/r1/interconnectAttachments/at1"
        )
        mock_peer = compute_v1.RouterBgpPeer(
            name="peer1",
            interface_name="if1",
            import_policies=[self.config.import_policy_name],
            export_policies=[self.config.export_policy_name]
        )
        mock_router = compute_v1.Router(
            name="rt1",
            interfaces=[mock_interface],
            bgp_peers=[mock_peer]
        )
        self.mock_router_client.get.return_value = mock_router
        
        mock_import_policy = compute_v1.RoutePolicy(name=self.config.import_policy_name)
        mock_export_policy = compute_v1.RoutePolicy(name=self.config.export_policy_name)
        self.mock_router_client.list_route_policies.return_value = [
            mock_import_policy, mock_export_policy
        ]

        results = self.orchestrator.cleanup_route_policies("p1")

        self.assertEqual(results, {("p1", "r1", "rt1"): {"success": True, "error": None}})
        
        self.mock_router_client.patch.assert_called()
        patched_router = self.mock_router_client.patch.call_args[1]["router_resource"]
        self.assertEqual(list(patched_router.bgp_peers[0].import_policies), [])
        self.assertEqual(list(patched_router.bgp_peers[0].export_policies), [])

        self.assertEqual(self.mock_router_client.delete_route_policy.call_count, 2)

class TestManualOverride(unittest.TestCase):
    def setUp(self):
        self.config = main.OrchestratorConfig()
        self.mock_ic_client = MagicMock()
        self.mock_attach_client = MagicMock()
        self.mock_router_client = MagicMock()
        
        self.orchestrator = main.MaintenanceOrchestrator(
            config=self.config,
            interconnects_client=self.mock_ic_client,
            attachments_client=self.mock_attach_client,
            routers_client=self.mock_router_client
        )

    def _setup_mock_link(self):
        mock_ic = compute_v1.Interconnect(
            name="ic-override",
            interconnect_attachments=["/projects/p1/regions/r1/interconnectAttachments/at1"]
        )
        self.mock_ic_client.list.return_value = [mock_ic]
        
        mock_attach = compute_v1.InterconnectAttachment(
            name="at1",
            router="https://www.googleapis.com/compute/v1/projects/p1/regions/r1/routers/rt1"
        )
        self.mock_attach_client.get.return_value = mock_attach

        mock_interface = compute_v1.RouterInterface(
            name="if1",
            linked_interconnect_attachment="https://www.googleapis.com/compute/v1/projects/p1/regions/r1/interconnectAttachments/at1"
        )
        mock_peer = compute_v1.RouterBgpPeer(
            name="peer1",
            interface_name="if1",
            import_policies=[],
            export_policies=[]
        )
        mock_router = compute_v1.Router(
            name="rt1",
            bgp=compute_v1.RouterBgp(asn=65001),
            interfaces=[mock_interface],
            bgp_peers=[mock_peer]
        )
        self.mock_router_client.get.return_value = mock_router
        self.mock_router_client.list_route_policies.return_value = []

    def test_manual_drain(self):
        self._setup_mock_link()
        
        self.orchestrator.manual_override_interconnect(
            target_ic_name="ic-override", enforce_drain=True, target_projects="p1"
        )
        
        self.mock_router_client.patch.assert_called()
        patched_router = self.mock_router_client.patch.call_args[1]["router_resource"]
        self.assertEqual(list(patched_router.bgp_peers[0].import_policies), [self.config.import_policy_name])
        self.assertEqual(list(patched_router.bgp_peers[0].export_policies), [self.config.export_policy_name])

    def test_manual_undrain(self):
        self._setup_mock_link()
        
        # Override initial peer state to represent an already drained peer
        mock_router = self.mock_router_client.get.return_value
        mock_router.bgp_peers[0].import_policies.append(self.config.import_policy_name)
        mock_router.bgp_peers[0].export_policies.append(self.config.export_policy_name)
        
        self.orchestrator.manual_override_interconnect(
            target_ic_name="ic-override", enforce_drain=False, target_projects="p1"
        )
        
        self.mock_router_client.patch.assert_called()
        patched_router = self.mock_router_client.patch.call_args[1]["router_resource"]
        self.assertEqual(list(patched_router.bgp_peers[0].import_policies), [])
        self.assertEqual(list(patched_router.bgp_peers[0].export_policies), [])

if __name__ == "__main__":
    unittest.main()
