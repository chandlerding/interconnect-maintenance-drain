import os
from dataclasses import dataclass, field
from typing import List, Tuple

@dataclass
class OrchestratorConfig:
    """Centralized configuration loaded from the environment."""
    projects: str = field(default_factory=lambda: os.environ.get("INTERCONNECT_PROJECTS", ""))
    lead_time_minutes: int = field(default_factory=lambda: int(os.environ.get("DRAIN_LEAD_TIME_MINUTES", "60")))
    no_op_policies: bool = field(default_factory=lambda: os.environ.get("NO_OP_POLICIES") == "1")
    import_policy_name: str = "interconnect-maintenance-drain-import"
    export_policy_name: str = "interconnect-maintenance-drain-export"
    wildcard_match_expr: str = "destination.inAnyRange([prefix('0.0.0.0/0').orLonger(), prefix('::/0').orLonger()])"
    import_policy_actions: Tuple[str, ...] = (
        "med.add(65535)",
        "asPath.prependSequence([{asn}, {asn}, {asn}, {asn}])"
    )
    export_policy_actions: Tuple[str, ...] = (
        "asPath.prependSequence([{asn}, {asn}, {asn}, {asn}])",
    )
    no_op_policy_actions: Tuple[str, ...] = (
        "nextPolicy()",
    )

@dataclass
class BgpPeerTarget:
    """Represents a targeted BGP peer and its desired state."""
    project_id: str
    region: str
    router_name: str
    peer_name: str
    target_policy_state: str
    is_drained_currently: bool

@dataclass
class InterconnectAuditResult:
    """Represents the audit result of an interconnect."""
    project_id: str
    interconnect: str
    outage_id: str
    target_state: str
    current_state: str
    action: str
    status: str
    associated_routers: List[Tuple[str, str, str]] = field(default_factory=list)
    _peer_targets: List[BgpPeerTarget] = field(default_factory=list)

@dataclass
class RouterReconciliationPlan:
    """Represents an executable reconciliation plan for a Cloud Router."""
    project_id: str
    region: str
    router_name: str
    peer_targets: List[BgpPeerTarget]
