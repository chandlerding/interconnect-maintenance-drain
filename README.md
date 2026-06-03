# GCP Interconnect Maintenance Drain Orchestrator

This tool automates the process of draining traffic from GCP Dedicated Interconnects before scheduled maintenance events, and restoring them to normal routing once maintenance is complete.

It mitigates packet loss and minimizes downtime during GCP-initiated maintenance by leveraging BGP Route Policies to increase MED values and prepend AS-Paths, gracefully steering traffic away from affected links before the physical link goes down.

## Features
*   **Automated Scanning**: Scans targeted GCP projects for Dedicated Interconnects and identifies active or imminent maintenance events (within a configurable lead time).
*   **Graceful Drain**: Dynamically applies import/export BGP Route Policies to affected BGP sessions, ensuring traffic is drained gracefully.
*   **Self-Healing & Restoration**: Automatically removes the drain policies once the maintenance window ends, restoring routing to normal.
*   **Optimistic Concurrency Control**: Uses atomic fetch-modify-patch retry loops with ETag fingerprints to handle concurrent updates safely.
*   **No-Op Mode (Testing)**: Supports deploying no-op policies (`nextPolicy()`) for safe validation of the API integration in live environments.

---

## Directory Structure

```
.
├── src/
│   ├── main.py          # Core orchestrator logic
│   ├── requirements.txt # Python dependencies
│   └── Dockerfile       # Container definition
├── terraform/           # Terraform deployment templates
│   ├── main.tf
│   ├── iam.tf
│   ├── variables.tf
│   ├── outputs.tf
│   └── README.md        # Deployment instructions
├── main_test.py         # Unit tests
├── integration_test.py  # Mock integration tests
└── live_simulation_test.py # Live environment simulation test (read-only/writes)
```

---

## Local Development & Testing

### 1. Setup Virtual Environment
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r src/requirements.txt
```

### 2. Running Unit Tests
Unit tests use mocks and do not require GCP access.
```bash
python3 main_test.py
```

### 3. Running Mock Integration Test
Verifies the end-to-end orchestration logic using a mock project topology.
```bash
python3 integration_test.py
```

### 4. Running Live Simulation Test
This test allows you to run the orchestrator against a real GCP project in a safe, read-only simulation mode, or optionally test real writes.

**Read-Only Simulation (Safe)**:
Reads the real topology from GCP, injects a simulated outage on a specified interconnect, and asserts that the orchestrator *would* have patched the routers correctly (without actually executing the writes).
```bash
LIVE_TEST_PROJECT="your-project-id" \
LIVE_TEST_INTERCONNECT="your-interconnect-name" \
python3 live_simulation_test.py
```

**Real-Write Validation (Warning: Modifies GCP Resources)**:
Triggers actual API calls to create the policies, apply them to the router's BGP peers, verifies the state, and then immediately rolls back the changes. To make this safe, combine it with `NO_OP_POLICIES=1` to write non-disruptive `nextPolicy()` rules.
```bash
NO_OP_POLICIES=1 \
LIVE_TEST_PROJECT="your-project-id" \
LIVE_TEST_INTERCONNECT="your-interconnect-name" \
LIVE_TEST_ENABLE_WRITES=1 \
python3 live_simulation_test.py
```

---

## Deployment

To deploy this tool as a scheduled task in GCP, see the [Terraform Deployment Guide](terraform/README.md).
