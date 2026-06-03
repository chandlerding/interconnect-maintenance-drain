# Enable required APIs in the Ops Project
resource "google_project_service" "apis" {
  for_each = toset([
    "compute.googleapis.com",
    "run.googleapis.com",
    "cloudscheduler.googleapis.com",
    "iam.googleapis.com",
    "artifactregistry.googleapis.com",
    "cloudbuild.googleapis.com",
  ])
  project            = var.ops_project_id
  service            = each.key
  disable_on_destroy = false
}

# Artifact Registry Repository for the Orchestrator image
resource "google_artifact_registry_repository" "orchestrator_repo" {
  project       = var.ops_project_id
  location      = var.region
  repository_id = var.repository_name
  description   = "Docker repository for Interconnect Drain Orchestrator"
  format        = "DOCKER"
  depends_on    = [google_project_service.apis]
}

# Cloud Run Job for the Orchestrator
resource "google_cloud_run_v2_job" "orchestrator_job" {
  project             = var.ops_project_id
  name                = "ic-maintenance-drain-orchestrator"
  location            = var.region
  deletion_protection = false

  template {
    template {
      service_account = google_service_account.job_sa.email
      containers {
        image = "${var.region}-docker.pkg.dev/${var.ops_project_id}/${var.repository_name}/${var.image_name}:${var.image_tag}"

        # Pass configuration via environment variables
        env {
          name  = "INTERCONNECT_PROJECTS"
          value = join(",", var.interconnect_project_ids)
        }
        env {
          name  = "DRAIN_LEAD_TIME_MINUTES"
          value = tostring(var.drain_lead_time_minutes)
        }
        env {
          name  = "NO_OP_POLICIES"
          value = var.no_op_policies ? "1" : "0"
        }
      }
    }
  }

  depends_on = [
    google_project_service.apis,
    google_service_account.job_sa,
    google_artifact_registry_repository.orchestrator_repo
  ]
}

# Cloud Scheduler to trigger the Cloud Run Job
resource "google_cloud_scheduler_job" "scheduler" {
  project          = var.ops_project_id
  name             = "ic-maintenance-drain-scheduler"
  region           = var.region
  schedule         = var.schedule
  time_zone        = "Etc/UTC"
  attempt_deadline = "320s"

  http_target {
    http_method = "POST"
    uri         = "https://${var.region}-run.googleapis.com/v2/${google_cloud_run_v2_job.orchestrator_job.id}:run"
    
    # Empty body is fine for triggering
    body        = base64encode("{}")

    headers = {
      "Content-Type" = "application/json"
    }

    oauth_token {
      service_account_email = google_service_account.scheduler_sa.email
    }
  }

  depends_on = [
    google_project_service.apis,
    google_cloud_run_v2_job.orchestrator_job,
    google_service_account.scheduler_sa,
    google_cloud_run_v2_job_iam_member.scheduler_invoker
  ]
}
