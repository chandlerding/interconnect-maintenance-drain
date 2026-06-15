# GCP Interconnect Maintenance Drain Orchestrator

This highly scalable SRE orchestration tool automates the process of draining traffic from GCP Dedicated Interconnects before scheduled maintenance events, and completely restoring them to normal routing once maintenance is complete.

It mitigates packet loss and minimizes downtime during GCP-initiated maintenance by leveraging Cloud Router BGP Route Policies to increase MED values and prepend AS-Paths, gracefully steering traffic away from affected physical circuits before the physical link goes down.

## Highly Modernized Features
*   **Asynchronous Multi-Threaded Scanning**: Highly concurrent multi-threaded Discovery (`ThreadPoolExecutor`) scans large enterprise projects instantly.
*   **Resilient API Backoffs**: Every single read (`.get()`/`.list()`) and write operation features an automated 5-attempt exponential backoff with randomized jitter (`random.uniform(0.1, 1.0)`), completely mitigating GCP `TooManyRequests` (HTTP 429) rate limit lockouts.
*   **Thread-Local Connection Pooling**: Employs an intelligent `threading.local()` client isolation architecture across all GCP API GAPIC SDKs (`Interconnects`, `Attachments`, `Routers`) to ensure zero `pyopenssl` SSLContext threading collisions.
*   **Graceful Network Drain**: Dynamically reconciles and injects import/export BGP Route Policies into affected Cloud Router BGP peer sessions.
*   **Self-Healing & Restoration**: Automatically strips drain policies once the active maintenance window expires, restoring normal routing.
*   **Automated Un-installation (`--cleanup-policies`)**: Cleanly strips and deletes all automated Route Policy resource definitions from Cloud Routers whenever you want a complete network reset.
*   **Emergency Manual Override (`--interconnect`, `--manual-drain`, `--manual-undrain`)**: Instantly enforce a precise maintenance DRAIN or restored NORMAL state on any specific Interconnect link during live incident response, bypassing dynamic maintenance notifications entirely.
*   **No-Op Mode (Testing)**: Supports deploying non-disruptive `nextPolicy()` rules (`NO_OP_POLICIES=1`) for 100% safe verification of the API integration in real live production environments.

---

## Modular Directory Structure

```
.
├── src/
│   ├── config.py        # Domain Data Models & Configuration Dataclasses
│   ├── utils.py         # Stateless String Parsers, Math & Summary Formatters
│   ├── orchestrator.py  # Multi-threaded Engine, Retries & Alignment Logic
│   ├── main.py          # Simplified CLI Entrypoint & Backward-Compatible Re-exports
│   ├── requirements.txt # Python dependencies
│   └── Dockerfile       # Fully optimized multi-module container definition
├── tests/
│   ├── main_test.py         # Unit tests (100% mocked, 25 comprehensive test suites)
│   ├── integration_test.py  # Mocked integration workflows proving Dependency Injection
│   └── live_simulation_test.py # Live environment simulation test (read-only/writes)
└── terraform/           # Terraform deployment templates
    ├── main.tf
    ├── iam.tf
    ├── variables.tf
    ├── outputs.tf
    └── README.md        # Deployment instructions
```

---

## SRE CLI Usage Guide

### Dynamic Maintenance Automation (Standard Mode)
Run the automated discovery engine against your configured or specified GCP projects:
```bash
python3 src/main.py --projects="tsunagu-interops,your-other-project"
```

### Emergency Incident Response (Manual Overrides)
Instantly intervene during live SRE incidents to manually force a network drain or undrain on a specific link:
```bash
# To force an immediate emergency maintenance DRAIN:
python3 src/main.py --projects="tsunagu-interops" --interconnect="bos17-interops-ex3400-01-hairpin-pri" --manual-drain

# To force an immediate live RESTORATION (un-drain):
python3 src/main.py --projects="tsunagu-interops" --interconnect="bos17-interops-ex3400-01-hairpin-pri" --manual-undrain
```

### Pristine Tool Un-installation (Policy Cleanup)
Cleanly unlink and permanently delete all Route Policy resources created by this tool across your networks:
```bash
python3 src/main.py --projects="tsunagu-interops" --cleanup-policies
```

---

## Local Development & Testing

### 1. Setup Virtual Environment
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r src/requirements.txt
```

### 2. Executing the Complete Test Suite
All 27 testing suites across unit, integration, and live simulations can be discovered and run effortlessly using Python's modern `unittest` runner:
```bash
python3 -m unittest discover -s tests -p "*_test.py"
```

### 3. Running Live Simulation Test Isolated
This test allows you to run the orchestrator against a real GCP project in a highly authentic, read-only simulation mode, or optionally test real writes.

**Read-Only Simulation (Safe)**:
Reads your real physical Interconnect topology from GCP, injects an authentic memory outage on the target link, and asserts that our thread-local factories *would* have aligned the route policies correctly (without executing real writes).
```bash
LIVE_TEST_PROJECT="your-project-id" \
LIVE_TEST_INTERCONNECT="your-interconnect-name" \
python3 tests/live_simulation_test.py
```

**Real-Write Validation (Warning: Modifies GCP Resources)**:
Triggers actual API calls to create the policies, apply them to the router's BGP peers, verifies the state, and then immediately rolls back the changes. To make this 100% safe, combine it with `NO_OP_POLICIES=1` to write non-disruptive `nextPolicy()` rules.
```bash
NO_OP_POLICIES=1 \
LIVE_TEST_PROJECT="your-project-id" \
LIVE_TEST_INTERCONNECT="your-interconnect-name" \
LIVE_TEST_ENABLE_WRITES=1 \
python3 tests/live_simulation_test.py
```

---

## Deployment

To deploy this tool as a rock-solid, fully autonomous scheduled task in GCP Cloud Run, see the [Terraform Deployment Guide](terraform/README.md).
