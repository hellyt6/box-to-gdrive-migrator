variable "aws_region" {
  description = "AWS region to deploy into"
  type        = string
  default     = "us-east-1"
}

variable "okta_org_url" {
  description = "Okta org base URL, e.g. https://yourcompany.okta.com"
  type        = string
}

variable "lambda_package_path" {
  description = "Path to the zipped Lambda deployment package (built from src/)"
  type        = string
  default     = "../build/migrate.zip"
}
