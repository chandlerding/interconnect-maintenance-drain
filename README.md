# GCP Interconnect Maintenance Drain Orchestrator

This SRE orchestration tool automates the process of draining traffic from GCP Dedicated Interconnects before scheduled maintenance events, and restoring them to normal routing once maintenance is complete.

It mitigates packet loss and minimizes downtime during GCP-initiated maintenance by leveraging Cloud Router BGP Route Policies to increase MED values and prepend AS-Paths, gracefully steering traffic away from affected physical circuits before the physical link goes down.

## Features
*   **Automated Maintenance Discovery**: Scans target GCP projects to identify Dedicated Interconnects with active or imminent maintenance events.
*   **Proactive Traffic Mitigation**: Gracefully steers traffic away from undergoing links by dynamically applying Cloud Router BGP Route Policies to increase MED and prepend AS-Paths before physical outages begin.
*   **Automated Restoration**: Automatically restores normal BGP routing patterns once scheduled maintenance windows conclude.
*   **Emergency SRE Override**: Allows operators to immediately bypass maintenance event schedules and force any specific Interconnect link into an active DRAIN or NORMAL (restored) state during live incident response.
*   **Complete Policy Cleanup**: Provides a clean un-installation mode to detach and delete all automated BGP Route Policy resources whenever you decide to decommission the tool.
*   **Safe Non-Disruptive Testing**: Supports deploying transparent `nextPolicy()` rules to safely validate end-to-end IAM and API integrations in live production networks.

---

## Directory Structure

```
.
├── src/
│   ├── config.py        # Domain Data Models & Configuration Dataclasses
│   ├── utils.py         # Stateless String Parsers, Math & Summary Formatters
│   ├── orchestrator.py  # Multi-threaded Engine, Retries & Alignment Logic
│   ├── main.py          # Simplified CLI Entrypoint & Re-exports
│   ├── requirements.txt # Python dependencies
│   └── Dockerfile       # Multi-module container definition
├── tests/
│   ├── main_test.py         # Unit tests (100% mocked, 25 test suites)
│   ├── integration_test.py  # Mocked integration workflows
│   └── live_simulation_test.py # Live environment simulation test (read-only/writes)
└── terraform/           # Terraform deployment templates
    ├── main.tf
    ├── iam.tf
    ├── variables.tf
    ├── outputs.tf
    └── README.md        # Deployment instructions
```

---

## CLI Usage Guide

### Dynamic Maintenance Automation (Standard Mode)
Run the automated discovery engine against your configured or specified GCP projects:
```bash
# Standard live execution:
python3 src/main.py --projects="your-production-project,your-staging-project"

# Safe non-disruptive validation (deploy transparent nextPolicy() rules):
python3 src/main.py --projects="your-production-project" --no-op-policies
```

### Emergency Incident Response (Manual Overrides)
Intervene during SRE incidents to force a network drain or un-drain on a specific link:
```bash
# To force an immediate emergency maintenance DRAIN:
python3 src/main.py --projects="your-production-project" --interconnect="your-interconnect-link-name" --manual-drain

# To force an immediate live RESTORATION (un-drain):
python3 src/main.py --projects="your-production-project" --interconnect="your-interconnect-link-name" --manual-undrain
```

### Tool Un-installation (Policy Cleanup)
Cleanly unlink and permanently delete all Route Policy resources created by this tool across your networks:
```bash
python3 src/main.py --projects="your-production-project" --cleanup-policies
```

---

## Local Development & Testing

### 1. Setup Virtual Environment
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r src/requirements.txt
```

### 2. Executing the Test Suite
All 27 testing suites across unit, integration, and live simulations can be run using Python's unified `unittest` runner:
```bash
python3 -m unittest discover -s tests -p "*_test.py"
```

### 3. Running Live Simulation Test Isolated
This test runs the orchestrator against a real GCP project in a safe, read-only simulation mode, or optionally tests real writes.

**Read-Only Simulation (Safe)**:
Reads your real physical Interconnect topology from GCP, injects a memory outage on the target link, and asserts that our thread-local factories would have aligned the route policies correctly (without executing real writes).
```bash
python3 -m tests.live_simulation_test --project="your-project-id" --interconnect="your-interconnect-name"
```

**Real-Write Validation (Warning: Modifies GCP Resources)**:
Triggers actual API calls to create the policies, apply them to the router's BGP peers, verifies the state, and immediately rolls back the changes. To make this completely safe, include the `--no-op-policies` flag to write non-disruptive `nextPolicy()` rules.
```bash
python3 -m tests.live_simulation_test --project="your-project-id" --interconnect="your-interconnect-name" --enable-writes --no-op-policies
```

---

## Deployment

To deploy this tool as an autonomous scheduled task in GCP Cloud Run, see the [Terraform Deployment Guide](terraform/README.md).
