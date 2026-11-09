terraform {
  required_version = ">= 1.3"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0, < 7.0"
    }
  }
}

variable "runbook_documents" {
  description = "Reviewed JSON from convert.py, keyed by custom SSM document name. Example: { QueueDiagnostics = file(\"queue-diagnostics.ssm.json\") }."
  type        = map(string)
  default     = {}
  nullable    = false

  validation {
    condition = alltrue([
      for name, body in var.runbook_documents :
      can(regex("^[A-Za-z0-9_.-]{3,128}$", name)) &&
      !startswith(lower(name), "aws") &&
      !startswith(lower(name), "amazon") &&
      !startswith(lower(name), "amzn") &&
      try(
        jsondecode(body).schemaVersion == "0.3" &&
        jsondecode(body).assumeRole == "{{ AutomationAssumeRole }}" &&
        jsondecode(body).parameters.AutomationAssumeRole.type == "String" &&
        !contains(keys(jsondecode(body).parameters.AutomationAssumeRole), "default") &&
        length(jsondecode(body).mainSteps) > 0,
        false
      )
    ])
    error_message = "Use custom SSM document names and valid Automation schema 0.3 JSON with steps and an explicit AutomationAssumeRole parameter."
  }
}

# Registers document definitions only. Review the converted JSON and scope the
# execution role in the calling environment before running a document.
resource "aws_ssm_document" "diagnostic" {
  for_each = var.runbook_documents

  name            = each.key
  document_format = "JSON"
  document_type   = "Automation"
  content         = each.value
}

output "document_arns" {
  description = "Published document ARNs keyed by input name."
  value       = { for name, document in aws_ssm_document.diagnostic : name => document.arn }
}
