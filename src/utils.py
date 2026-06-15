import re
import logging
from datetime import datetime, timezone
from typing import List, Tuple
try:
    from .config import InterconnectAuditResult
except ImportError:
    from config import InterconnectAuditResult

def is_peer_aligned(target_state: str, is_drained: bool) -> bool:
    """Evaluates whether a BGP peer's active drain policies match its target state."""
    return (target_state == "DRAINED" and is_drained) or (target_state == "NORMAL" and not is_drained)

def parse_epoch_ms(ts_val) -> datetime:
    """Converts GCP Interconnect maintenance epoch milliseconds into a UTC datetime object."""
    if not ts_val:
         return None
    try:
         return datetime.fromtimestamp(int(ts_val) / 1000.0, tz=timezone.utc)
    except (ValueError, TypeError) as e:
         raise ValueError(f"CRITICAL: Unable to parse maintenance timestamp '{ts_val}'. Schema may have changed: {e}")

def parse_attachment_url(url: str) -> Tuple[str, str, str]:
    """Extracts project ID, region, and attachment name from an Interconnect Attachment resource URI."""
    pattern = r"/projects/([^/]+)/regions/([^/]+)/interconnectAttachments/([^/]+)$"
    match = re.search(pattern, url)
    if not match:
        raise ValueError(f"Unrecognized Interconnect Attachment resource URI structure: {url}")
    return match.group(1), match.group(2), match.group(3)

def log_sre_summary_table(records: List[InterconnectAuditResult]):
    """Renders a GitHub markdown formatted summary table of all maintenance operations."""
    if not records:
        logging.info("\n### GCI MAINTENANCE EVENTS SUMMARY: ZERO INTERCONNECTS DETECTED")
        return

    lines = []
    lines.append("\n### GCI Routing Maintenance Events Summary Run")
    lines.append("| PROJECT ID | INTERCONNECT | ACTIVE OUTAGE | TARGET STATE | CURRENT STATE | ACTION TAKEN | RESULT STATUS |")
    lines.append("| :--- | :--- | :--- | :--- | :--- | :--- | :--- |")
    
    for r in records:
         row = "| {} | {} | {} | {} | {} | {} | {} |".format(
             r.project_id,
             r.interconnect,
             r.outage_id,
             r.target_state,
             r.current_state,
             r.action,
             r.status
         )
         lines.append(row)
         
    lines.append("\n")
    logging.info("\n".join(lines))
