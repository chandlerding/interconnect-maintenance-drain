import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock
from src import main

class TestParseRFC3339(unittest.TestCase):

    def test_parse_valid_rfc3339_string_z(self):
        ts = "2026-06-03T01:37:17Z"
        expected = datetime(2026, 6, 3, 1, 37, 17, tzinfo=timezone.utc)
        self.assertEqual(main.parse_rfc3339(ts), expected)

    def test_parse_valid_rfc3339_string_timezone(self):
        ts = "2026-06-03T01:37:17+02:00"
        expected = datetime(2026, 6, 3, 1, 37, 17, tzinfo=timezone(timedelta(hours=2)))
        self.assertEqual(main.parse_rfc3339(ts), expected)

    def test_parse_epoch_milliseconds_int(self):
        ts = 1776735000000
        expected = datetime(2026, 4, 21, 1, 30, 0, tzinfo=timezone.utc)
        self.assertEqual(main.parse_rfc3339(ts), expected)

    def test_parse_epoch_milliseconds_str(self):
        ts = "1776735000000"
        expected = datetime(2026, 4, 21, 1, 30, 0, tzinfo=timezone.utc)
        self.assertEqual(main.parse_rfc3339(ts), expected)

    def test_parse_empty_value(self):
        self.assertIsNone(main.parse_rfc3339(None))
        self.assertIsNone(main.parse_rfc3339(""))

    def test_parse_invalid_format(self):
        with self.assertRaises(ValueError):
            main.parse_rfc3339("invalid-date")

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
    def test_update_success(self):
        run_summary = [
            {"status": "PENDING_ALIGNMENT", "associated_routers": [("p1", "r1", "rt1")]}
        ]
        results = {("p1", "r1", "rt1"): {"success": True}}
        main._update_final_statuses(run_summary, results)
        self.assertEqual(run_summary[0]["status"], "SUCCESS")

    def test_update_failed(self):
        run_summary = [
            {"status": "PENDING_ALIGNMENT", "associated_routers": [("p1", "r1", "rt1")], "action": "DRAINED"}
        ]
        results = {("p1", "r1", "rt1"): {"success": False, "error": "some error"}}
        main._update_final_statuses(run_summary, results)
        self.assertEqual(run_summary[0]["status"], "FAILED: some error")
        self.assertEqual(run_summary[0]["action"], "ERROR")

    def test_update_no_change_if_not_pending(self):
        run_summary = [
            {"status": "SUCCESS: NO_ATTACHMENTS", "associated_routers": []}
        ]
        results = {}
        main._update_final_statuses(run_summary, results)
        self.assertEqual(run_summary[0]["status"], "SUCCESS: NO_ATTACHMENTS")

class TestEvaluateAndConsolidate(unittest.TestCase):
    def test_no_change_needed(self):
        run_summary = [
            {"target_state": "NORMAL", "status": "SUCCESS", "_peer_targets": [
                {"project_id": "p1", "region": "r1", "router_name": "rt1", "peer_name": "gp1", "target_policy_state": "NORMAL", "is_drained_currently": False}
            ]}
        ]
        routers_to_align = {
            ("p1", "r1", "rt1"): {
                "gp1": {"target_policy_state": "NORMAL", "is_drained_currently": False}
            }
        }
        filtered = main._evaluate_and_consolidate(run_summary, routers_to_align)
        self.assertEqual(filtered, {})
        self.assertEqual(run_summary[0]["action"], "NO_ACTION")
        self.assertEqual(run_summary[0]["status"], "SUCCESS")
        self.assertNotIn("_peer_targets", run_summary[0])

    def test_alignment_needed_drain(self):
        run_summary = [
            {"target_state": "DRAINED", "status": "SUCCESS", "_peer_targets": [
                {"project_id": "p1", "region": "r1", "router_name": "rt1", "peer_name": "gp1", "target_policy_state": "DRAINED", "is_drained_currently": False}
            ]}
        ]
        routers_to_align = {
            ("p1", "r1", "rt1"): {
                "gp1": {"target_policy_state": "DRAINED", "is_drained_currently": False}
            }
        }
        filtered = main._evaluate_and_consolidate(run_summary, routers_to_align)
        self.assertIn(("p1", "r1", "rt1"), filtered)
        self.assertEqual(run_summary[0]["action"], "DRAINED")
        self.assertEqual(run_summary[0]["status"], "PENDING_ALIGNMENT")

    def test_alignment_needed_restore(self):
        run_summary = [
            {"target_state": "NORMAL", "status": "SUCCESS", "_peer_targets": [
                {"project_id": "p1", "region": "r1", "router_name": "rt1", "peer_name": "gp1", "target_policy_state": "NORMAL", "is_drained_currently": True}
            ]}
        ]
        routers_to_align = {
            ("p1", "r1", "rt1"): {
                "gp1": {"target_policy_state": "NORMAL", "is_drained_currently": True}
            }
        }
        filtered = main._evaluate_and_consolidate(run_summary, routers_to_align)
        self.assertIn(("p1", "r1", "rt1"), filtered)
        self.assertEqual(run_summary[0]["action"], "RESTORED")
        self.assertEqual(run_summary[0]["status"], "PENDING_ALIGNMENT")

class TestAuditProjects(unittest.TestCase):
    @patch('src.main.interconnects_client')
    @patch('src.main.process_interconnect_maintenance_events')
    def test_audit_success(self, mock_process, mock_ic_client):
        mock_ic = MagicMock()
        mock_ic.name = "ic1"
        mock_ic_client.list.return_value = [mock_ic]
        
        mock_process.return_value = (
            {"project_id": "p1", "interconnect": "ic1", "status": "SUCCESS", "target_state": "DRAINED"},
            [{"project_id": "p1", "region": "r1", "router_name": "rt1", "peer_name": "gp1", "target_policy_state": "DRAINED", "is_drained_currently": False}]
        )
        
        run_summary, routers_to_align, failed_projects = main._audit_projects(["p1"])
        
        self.assertEqual(len(run_summary), 1)
        self.assertEqual(run_summary[0]["interconnect"], "ic1")
        self.assertIn(("p1", "r1", "rt1"), routers_to_align)
        self.assertEqual(len(failed_projects), 0)

    @patch('src.main.interconnects_client')
    def test_audit_forbidden(self, mock_ic_client):
        from google.api_core import exceptions as gcp_exceptions
        mock_ic_client.list.side_effect = gcp_exceptions.Forbidden("Forbidden")
        
        run_summary, routers_to_align, failed_projects = main._audit_projects(["p1"])
        
        self.assertEqual(len(run_summary), 1)
        self.assertEqual(run_summary[0]["status"], "FAILED: IAM_PERMISSION_DENIED")
        self.assertEqual(len(failed_projects), 1)
        self.assertIn("p1", failed_projects)

class TestProcessMaintenanceEvents(unittest.TestCase):
    @patch('src.main._audit_projects')
    @patch('src.main._evaluate_and_consolidate')
    @patch('src.main._align_routers_parallel')
    @patch('src.main._update_final_statuses')
    @patch('src.main.log_sre_summary_table')
    def test_process_success(self, mock_log, mock_update, mock_align, mock_evaluate, mock_audit):
        mock_audit.return_value = ([], {}, set())
        mock_evaluate.return_value = {}
        mock_align.return_value = {}
        
        main.process_maintenance_events(target_projects="p1")
        
        mock_audit.assert_called_once_with(["p1"])
        mock_evaluate.assert_called_once()
        mock_align.assert_called_once()
        mock_update.assert_called_once()
        mock_log.assert_called_once()

    @patch('src.main._audit_projects')
    def test_process_no_projects(self, mock_audit):
        with patch.dict('os.environ', {'INTERCONNECT_PROJECTS': ''}):
            with self.assertRaises(ValueError):
                main.process_maintenance_events()

class TestProcessInterconnectMaintenanceEvents(unittest.TestCase):
    @patch('src.main.check_current_bgp_states')
    def test_no_outages(self, mock_check_bgp):
        mock_ic = MagicMock()
        mock_ic.name = "ic1"
        mock_ic.expected_outages = []
        mock_ic.interconnect_attachments = ["/projects/p1/regions/r1/interconnectAttachments/at1"]
        
        mock_check_bgp.return_value = [
            {"project_id": "p1", "region": "r1", "router_name": "rt1", "peer_name": "gp1", "is_drained": False}
        ]
        
        record, peer_targets = main.process_interconnect_maintenance_events("p1", mock_ic, {})
        
        self.assertEqual(record["target_state"], "NORMAL")
        self.assertEqual(record["current_state"], "NORMAL")
        self.assertEqual(len(peer_targets), 1)
        self.assertEqual(peer_targets[0]["target_policy_state"], "NORMAL")

    @patch('src.main.check_current_bgp_states')
    def test_active_outage(self, mock_check_bgp):
        mock_ic = MagicMock()
        mock_ic.name = "ic1"
        
        mock_outage = MagicMock()
        mock_outage.name = "out1"
        mock_outage.state = "ACTIVE"
        now = datetime.now(timezone.utc)
        mock_outage.start_time = (now - timedelta(minutes=10)).isoformat()
        mock_outage.end_time = (now + timedelta(minutes=50)).isoformat()
        mock_ic.expected_outages = [mock_outage]
        mock_ic.interconnect_attachments = ["/projects/p1/regions/r1/interconnectAttachments/at1"]
        
        mock_check_bgp.return_value = [
            {"project_id": "p1", "region": "r1", "router_name": "rt1", "peer_name": "gp1", "is_drained": False}
        ]
        
        record, peer_targets = main.process_interconnect_maintenance_events("p1", mock_ic, {})
        
        self.assertEqual(record["target_state"], "DRAINED")
        self.assertEqual(record["current_state"], "NORMAL")
        self.assertEqual(len(peer_targets), 1)
        self.assertEqual(peer_targets[0]["target_policy_state"], "DRAINED")

    @patch('src.main.check_current_bgp_states')
    def test_imminent_outage(self, mock_check_bgp):
        mock_ic = MagicMock()
        mock_ic.name = "ic1"
        
        mock_outage = MagicMock()
        mock_outage.name = "out1"
        mock_outage.state = "ACTIVE"
        now = datetime.now(timezone.utc)
        mock_outage.start_time = (now + timedelta(minutes=30)).isoformat()
        mock_outage.end_time = (now + timedelta(minutes=90)).isoformat()
        mock_ic.expected_outages = [mock_outage]
        mock_ic.interconnect_attachments = ["/projects/p1/regions/r1/interconnectAttachments/at1"]
        
        mock_check_bgp.return_value = [
            {"project_id": "p1", "region": "r1", "router_name": "rt1", "peer_name": "gp1", "is_drained": False}
        ]
        
        record, peer_targets = main.process_interconnect_maintenance_events("p1", mock_ic, {})
        
        self.assertEqual(record["target_state"], "DRAINED")
        
    @patch('src.main.check_current_bgp_states')
    def test_future_outage_not_imminent(self, mock_check_bgp):
        mock_ic = MagicMock()
        mock_ic.name = "ic1"
        
        mock_outage = MagicMock()
        mock_outage.name = "out1"
        mock_outage.state = "ACTIVE"
        now = datetime.now(timezone.utc)
        mock_outage.start_time = (now + timedelta(minutes=120)).isoformat()
        mock_outage.end_time = (now + timedelta(minutes=180)).isoformat()
        mock_ic.expected_outages = [mock_outage]
        mock_ic.interconnect_attachments = ["/projects/p1/regions/r1/interconnectAttachments/at1"]
        
        mock_check_bgp.return_value = [
            {"project_id": "p1", "region": "r1", "router_name": "rt1", "peer_name": "gp1", "is_drained": False}
        ]
        
        record, peer_targets = main.process_interconnect_maintenance_events("p1", mock_ic, {})
        
        self.assertEqual(record["target_state"], "NORMAL")

if __name__ == "__main__":
    unittest.main()
