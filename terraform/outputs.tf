output "job_service_account_email" {
  value       = google_service_account.job_sa.email
  description = "The service account email used by the Cloud Run Job."
}

output "scheduler_service_account_email" {
  value       = google_service_account.scheduler_sa.email
  description = "The service account email used by the Cloud Scheduler."
}

output "cloud_run_job_name" {
  value       = google_cloud_run_v2_job.orchestrator_job.name
  description = "The name of the deployed Cloud Run Job."
}

output "cloud_scheduler_job_name" {
  value       = google_cloud_scheduler_job.scheduler.name
  description = "The name of the deployed Cloud Scheduler Job."
}
