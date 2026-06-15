# Service Account for Cloud Run Job (Runtime Identity)
resource "google_service_account" "job_sa" {
  project      = var.ops_project_id
  account_id   = "ic-drain-job-sa"
  display_name = "Interconnect Drain Orchestrator Job Service Account"
  depends_on   = [google_project_service.apis]
}

# Service Account for Cloud Scheduler (Triggering Identity)
resource "google_service_account" "scheduler_sa" {
  project      = var.ops_project_id
  account_id   = "ic-drain-scheduler-sa"
  display_name = "Interconnect Drain Orchestrator Scheduler Service Account"
  depends_on   = [google_project_service.apis]
}



locals {
  use_org = var.org_id != ""
  # Deduplicate projects for read-only viewer role
  all_target_projects = local.use_org ? [] : distinct(concat(var.interconnect_project_ids, var.vlan_attachment_project_ids))
}

# ==============================================================================
# AUTOMATIC PERMISSION RESOLUTION
# The template automatically toggles between Option 1 (Project-level bindings)
# and Option 2 (Organization-wide bindings) depending on whether `var.org_id` is populated.
# Users do NOT need to modify this file to choose an option.
# ==============================================================================

# OPTION 1: Project-level permissions (Active when var.org_id is empty)

# Custom Role created locally in each target project (Option 1)
resource "google_project_iam_custom_role" "router_policy_editor_proj" {
  for_each    = local.use_org ? [] : toset(var.vlan_attachment_project_ids)
  project     = each.key
  role_id     = "RouterPolicyEditor"
  title       = "Router Policy Editor"
  description = "Minimal permissions to update Cloud Router route policies and BGP peers"
  permissions = [
    "compute.routers.get",
    "compute.routers.update",
    "compute.routers.updateRoutePolicy",
    "compute.routers.deleteRoutePolicy",
  ]
}

# Grant Compute Viewer on all target projects (Read-only access)
resource "google_project_iam_member" "viewer_proj" {
  for_each = local.use_org ? [] : toset(local.all_target_projects)
  project  = each.key
  role     = "roles/compute.viewer"
  member   = "serviceAccount:${google_service_account.job_sa.email}"
}

# Grant Router Policy Editor on VLAN attachment projects (Write access)
resource "google_project_iam_member" "editor_proj" {
  for_each = local.use_org ? [] : toset(var.vlan_attachment_project_ids)
  project  = each.key
  role     = google_project_iam_custom_role.router_policy_editor_proj[each.key].id
  member   = "serviceAccount:${google_service_account.job_sa.email}"
}

# OPTION 2: Org-level permissions (Active when var.org_id is set)

# Custom Role created at the Org level (Option 2)
resource "google_organization_iam_custom_role" "router_policy_editor_org" {
  count       = local.use_org ? 1 : 0
  org_id      = var.org_id
  role_id     = "RouterPolicyEditor"
  title       = "Router Policy Editor"
  description = "Minimal permissions to update Cloud Router route policies and BGP peers"
  permissions = [
    "compute.routers.get",
    "compute.routers.update",
    "compute.routers.updateRoutePolicy",
    "compute.routers.deleteRoutePolicy",
  ]
}

# Grant Compute Viewer at Org Level
resource "google_organization_iam_member" "viewer_org" {
  count  = local.use_org ? 1 : 0
  org_id = var.org_id
  role   = "roles/compute.viewer"
  member = "serviceAccount:${google_service_account.job_sa.email}"
}

# Grant Router Policy Editor at Org Level (using the Org-level custom role)
resource "google_organization_iam_member" "editor_org" {
  count  = local.use_org ? 1 : 0
  org_id = var.org_id
  role   = google_organization_iam_custom_role.router_policy_editor_org[0].id
  member = "serviceAccount:${google_service_account.job_sa.email}"
}

# ==========================================
# Scheduler Invocation Permissions
# ==========================================

# Allow Scheduler SA to run the Cloud Run Job
resource "google_cloud_run_v2_job_iam_member" "scheduler_invoker" {
  project  = var.ops_project_id
  location = var.region
  name     = google_cloud_run_v2_job.orchestrator_job.name
  role     = "roles/run.jobsExecutor"
  member   = "serviceAccount:${google_service_account.scheduler_sa.email}"
}
