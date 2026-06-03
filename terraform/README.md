# Deploying Interconnect Maintenance Drain Orchestrator

This Terraform template deploys the orchestrator as a **Cloud Run Job** in a designated "operations" project, scheduled to run periodically (e.g., every 15 minutes) via **Cloud Scheduler**.

## Infrastructure Overview

The template provisions:
1.  **Required APIs**: Enables Compute Engine, Cloud Run, Cloud Scheduler, IAM, Artifact Registry, and Cloud Build APIs in the ops project.
2.  **Service Accounts**:
    *   `ic-drain-job-sa`: The runtime identity used by the Cloud Run Job.
    *   `ic-drain-scheduler-sa`: The identity used by Cloud Scheduler to trigger the Cloud Run Job.
3.  **Custom IAM Role**: `RouterPolicyEditor` containing minimal permissions required to update Cloud Router route policies and BGP peers (`compute.routers.update`, `compute.routers.updateRoutePolicy`, `compute.routers.get`). Depending on your configuration, it is created locally in each target project (Option 1) or at the Organization level (Option 2).
4.  **Cloud Run Job**: Packages the orchestrator and configures environment variables (`INTERCONNECT_PROJECTS`, `DRAIN_LEAD_TIME_MINUTES`, `NO_OP_POLICIES`).
5.  **Cloud Scheduler**: Triggers the Cloud Run Job at the specified interval.

---

## Step 1: Configure Terraform Variables

Create a `terraform.tfvars` file in this directory. There are **two ways to configure permissions** for the orchestrator:

### Option 1: Grant permissions per target project (Recommended for least privilege)
Use this option to explicitly list the projects containing your Interconnects and VLAN attachments.

```hcl
ops_project_id              = "ops-project-123"
region                      = "us-central1"
schedule                    = "*/15 * * * *" # Every 15 minutes
repository_name             = "ic-drain-repo"
image_name                  = "orchestrator"
image_tag                   = "latest"
drain_lead_time_minutes     = 60
no_op_policies               = false # Set to true for safe testing with no-op policies

# Target projects configuration
interconnect_project_ids    = ["interconnect-host-project"]
vlan_attachment_project_ids = ["spoke-project-a", "spoke-project-b"]
```

### Option 2: Grant organization-wide permissions (Easier setup for large scale)
Use this option if you want the orchestrator to automatically have access to all projects in the organization.

```hcl
ops_project_id              = "ops-project-123"
region                      = "us-central1"
schedule                    = "*/15 * * * *"
repository_name             = "ic-drain-repo"
image_name                  = "orchestrator"
image_tag                   = "latest"
drain_lead_time_minutes     = 60
no_op_policies               = false

# Target projects configuration
interconnect_project_ids    = ["interconnect-host-project"]
org_id                      = "123456789012" # Your GCP Organization ID
```
*Note: If `org_id` is provided, `vlan_attachment_project_ids` is ignored, and permissions are bound at the organization level.*

---

## Step 2: Deploy

Because the Cloud Run Job requires the container image to exist during deployment, but the Artifact Registry repository is managed by Terraform, we use a **two-phase bootstrap deployment**:

### Phase 1: Bootstrap the Repository and APIs

1.  Initialize Terraform:
    ```bash
    terraform init
    ```
2.  Deploy **only** the APIs and the Artifact Registry repository:
    ```bash
    terraform apply \
      -target=google_artifact_registry_repository.orchestrator_repo \
      -target=google_project_service.apis
    ```

### Phase 2: Build, Push, and Deploy

1.  Build the Docker image and push it to the newly created repository using Cloud Build:
    ```bash
    gcloud builds submit --tag us-central1-docker.pkg.dev/YOUR_OPS_PROJECT_ID/ic-drain-repo/orchestrator:latest src
    ```
    *(Replace `YOUR_OPS_PROJECT_ID`, `ic-drain-repo`, and `orchestrator` with your configured variables).*

2.  Deploy the remaining resources (Cloud Run Job, Cloud Scheduler, and IAM permissions):
    ```bash
    terraform apply
    ```

---

## Rollback / Deletion

To tear down the deployed resources, run:
```bash
terraform destroy
```
*Note: Any route policies created by the orchestrator on live Cloud Routers will NOT be deleted by Terraform. If you want to clean them up, run the orchestrator once with no outages active to ensure it removes the policy associations from BGP peers, then you can manually delete the policies from the routers if desired.*

---

## Validation and Testing

To verify the Terraform configuration without actually deploying resources to GCP, you can run the following validation pipeline:

1.  **Format Check**:
    Verify that all files conform to canonical Terraform style:
    ```bash
    terraform fmt -check
    ```
    *(Run `terraform fmt` to automatically fix any formatting issues).*

2.  **Initialization**:
    Initialize the Terraform working directory and download the Google provider plugins:
    ```bash
    terraform init
    ```

3.  **Validation**:
    Perform a static analysis of the configuration files to ensure syntax validity and internal consistency:
    ```bash
    terraform validate
    ```

4.  **Dry Run (Plan)**:
    Generate a preview of the resources Terraform will create. You can test this using dummy variables without needing real GCP access:
    ```bash
    terraform plan \
      -var="ops_project_id=dummy-ops-project" \
      -var="interconnect_project_ids=[\"dummy-ic-project\"]" \
      -var="vlan_attachment_project_ids=[\"dummy-spoke-project\"]"
    ```

    To test the **Organization-wide** path (Option 2):
    ```bash
    terraform plan \
      -var="ops_project_id=dummy-ops-project" \
      -var="interconnect_project_ids=[\"dummy-ic-project\"]" \
      -var="org_id=123456789012"
    ```

