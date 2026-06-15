import os
import sys
import logging
from typing import List, Tuple
from datetime import datetime, timezone, timedelta
from google.cloud import compute_v1

try:
    from .config import OrchestratorConfig, BgpPeerTarget, InterconnectAuditResult, RouterReconciliationPlan
    from .utils import parse_epoch_ms, parse_attachment_url, is_peer_aligned, log_sre_summary_table
    from .orchestrator import MaintenanceOrchestrator
except ImportError:
    from config import OrchestratorConfig, BgpPeerTarget, InterconnectAuditResult, RouterReconciliationPlan
    from utils import parse_epoch_ms, parse_attachment_url, is_peer_aligned, log_sre_summary_table
    from orchestrator import MaintenanceOrchestrator

logging.basicConfig(level=logging.INFO, format="%(asctime)s - [%(levelname)s] - %(message)s")

def process_maintenance_events(target_projects: str = "", no_op_policies: bool = False):
    """Top-level execution wrapper for backward compatibility."""
    config = OrchestratorConfig()
    if no_op_policies:
        config.no_op_policies = True
    orchestrator = MaintenanceOrchestrator(config)
    orchestrator.process_maintenance_events(target_projects)

def cleanup_all_route_policies(target_projects: str = ""):
    """Top-level execution wrapper for policy cleanup feature."""
    config = OrchestratorConfig()
    orchestrator = MaintenanceOrchestrator(config)
    orchestrator.cleanup_route_policies(target_projects)

def execute_manual_override(interconnect_name: str, enforce_drain: bool, target_projects: str = "", no_op_policies: bool = False):
    """Top-level execution wrapper for emergency manual interconnect override feature."""
    config = OrchestratorConfig()
    if no_op_policies:
        config.no_op_policies = True
    orchestrator = MaintenanceOrchestrator(config)
    orchestrator.manual_override_interconnect(interconnect_name, enforce_drain, target_projects)

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Drain/undrain Interconnect connections before/after maintenance events, by applying bgp routing policy on affected bgp sessions")
    parser.add_argument(
        "--projects",
        default="",
        help="Optional: Comma-separated list of GCP Project IDs to scan. If omitted, falls back to config.projects."
    )
    parser.add_argument(
        "--no-op-policies",
        action="store_true",
        help="Optional: Deploy non-disruptive nextPolicy() BGP route rules rather than active MED/AS-Path prepends. Note: This still executes actual API write calls to GCP Cloud Routers."
    )
    parser.add_argument(
        "--cleanup-policies",
        action="store_true",
        help="Optional: Run in un-installation mode to cleanly strip and delete all automated BGP route policies created by this tool."
    )
    parser.add_argument(
        "--interconnect",
        default="",
        help="Optional: Target a specific Interconnect link by exact name for manual override operations."
    )
    parser.add_argument(
        "--manual-drain",
        action="store_true",
        help="Optional: Enforce an immediate DRAIN state on the specified --interconnect, bypassing maintenance event notifications."
    )
    parser.add_argument(
        "--manual-undrain",
        action="store_true",
        help="Optional: Enforce an immediate NORMAL (restored) state on the specified --interconnect, bypassing maintenance event notifications."
    )
    args = parser.parse_args()
    
    if (args.manual_drain or args.manual_undrain) and not args.interconnect:
        parser.error("--manual-drain and --manual-undrain flags strictly require an explicit --interconnect argument.")
    if args.manual_drain and args.manual_undrain:
        parser.error("--manual-drain and --manual-undrain flags are mutually exclusive.")
        
    if args.cleanup_policies:
        cleanup_all_route_policies(target_projects=args.projects)
    elif args.manual_drain or args.manual_undrain:
        execute_manual_override(
            interconnect_name=args.interconnect, 
            enforce_drain=args.manual_drain, 
            target_projects=args.projects,
            no_op_policies=args.no_op_policies
        )
    else:
        process_maintenance_events(
            target_projects=args.projects,
            no_op_policies=args.no_op_policies
        )
