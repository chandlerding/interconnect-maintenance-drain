variable "ops_project_id" {
  type        = string
  description = "The GCP Project ID where the Cloud Run Task and Scheduler will be deployed."
}

variable "region" {
  type        = string
  default     = "us-central1"
  description = "The GCP region where the Cloud Run Job and Scheduler will be deployed."
}

variable "interconnect_project_ids" {
  type        = list(string)
  description = "List of GCP Project IDs containing the physical Interconnects to scan."
}

variable "vlan_attachment_project_ids" {
  type        = list(string)
  default     = []
  description = "Option 1: List of GCP Project IDs containing VLAN attachments and Cloud Routers. Ignored if org_id is set."
}

variable "org_id" {
  type        = string
  default     = ""
  description = "Option 2: Google Cloud Organization ID to grant org-wide permissions. If set, vlan_attachment_project_ids is ignored."
}

variable "schedule" {
  type        = string
  default     = "*/15 * * * *"
  description = "Cron schedule for the Cloud Run Task execution (default: every 15 minutes)."
}

variable "repository_name" {
  type        = string
  default     = "ic-drain-repo"
  description = "The name of the Artifact Registry repository containing the orchestrator image."
}

variable "image_name" {
  type        = string
  default     = "orchestrator"
  description = "The name of the orchestrator image."
}

variable "image_tag" {
  type        = string
  default     = "latest"
  description = "The tag of the orchestrator image."
}

variable "drain_lead_time_minutes" {
  type        = number
  default     = 60
  description = "Pre-maintenance lead time in minutes."
}

variable "no_op_policies" {
  type        = bool
  default     = false
  description = "If true, deploys route policies with nextPolicy() (no-op) for safe testing."
}
