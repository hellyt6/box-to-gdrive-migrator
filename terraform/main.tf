terraform {
  required_version = ">= 1.5.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

# ---------------------------------------------------------------------------
# Secrets: Okta API token, Box JWT service account config, Google service
# account key. Values are managed out-of-band (aws secretsmanager put-secret-value)
# rather than in Terraform state, to avoid credentials ever touching the
# state file.
# ---------------------------------------------------------------------------

resource "aws_secretsmanager_secret" "okta_api_token" {
  name = "box-gdrive-migrator/okta-api-token"
}

resource "aws_secretsmanager_secret" "box_jwt_config" {
  name = "box-gdrive-migrator/box-jwt-config"
}

resource "aws_secretsmanager_secret" "google_service_account" {
  name = "box-gdrive-migrator/google-service-account"
}

# ---------------------------------------------------------------------------
# Migration manifest table — tracks migrated file checksums for idempotency
# ---------------------------------------------------------------------------

resource "aws_dynamodb_table" "migration_manifest" {
  name         = "box-gdrive-migration-manifest"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "box_file_id"

  attribute {
    name = "box_file_id"
    type = "S"
  }

  tags = {
    Project = "box-to-gdrive-migrator"
  }
}

# ---------------------------------------------------------------------------
# Least-privilege IAM role for the Lambda: only the specific secrets, the
# manifest table, and log writes. No wildcard resource grants.
# ---------------------------------------------------------------------------

resource "aws_iam_role" "lambda_exec" {
  name = "box-gdrive-migrator-lambda-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy" "lambda_policy" {
  name = "box-gdrive-migrator-lambda-policy"
  role = aws_iam_role.lambda_exec.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "SecretsAccess"
        Effect = "Allow"
        Action = ["secretsmanager:GetSecretValue"]
        Resource = [
          aws_secretsmanager_secret.okta_api_token.arn,
          aws_secretsmanager_secret.box_jwt_config.arn,
          aws_secretsmanager_secret.google_service_account.arn,
        ]
      },
      {
        Sid    = "ManifestTableAccess"
        Effect = "Allow"
        Action = ["dynamodb:GetItem", "dynamodb:PutItem"]
        Resource = aws_dynamodb_table.migration_manifest.arn
      },
      {
        Sid      = "Logging"
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:${var.aws_region}:*:log-group:/aws/lambda/box-gdrive-migrator*"
      }
    ]
  })
}

# ---------------------------------------------------------------------------
# Lambda function
# ---------------------------------------------------------------------------

resource "aws_lambda_function" "migrator" {
  function_name = "box-gdrive-migrator"
  role          = aws_iam_role.lambda_exec.arn
  handler       = "migrate.lambda_handler"
  runtime       = "python3.12"
  timeout       = 900 # 15 min Lambda max — see README known limitations
  memory_size   = 512

  filename         = var.lambda_package_path
  source_code_hash = filebase64sha256(var.lambda_package_path)

  environment {
    variables = {
      OKTA_ORG_URL              = var.okta_org_url
      MANIFEST_TABLE             = aws_dynamodb_table.migration_manifest.name
      OKTA_API_TOKEN_SECRET_ARN  = aws_secretsmanager_secret.okta_api_token.arn
      BOX_JWT_CONFIG_SECRET_ARN  = aws_secretsmanager_secret.box_jwt_config.arn
      GOOGLE_SA_SECRET_ARN       = aws_secretsmanager_secret.google_service_account.arn
    }
  }
}

# ---------------------------------------------------------------------------
# Scheduled trigger — nightly incremental sync
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_event_rule" "nightly_sync" {
  name                = "box-gdrive-migrator-nightly"
  schedule_expression = "cron(0 6 * * ? *)" # 6am UTC daily
}

resource "aws_cloudwatch_event_target" "lambda_target" {
  rule = aws_cloudwatch_event_rule.nightly_sync.name
  arn  = aws_lambda_function.migrator.arn
}

resource "aws_lambda_permission" "allow_eventbridge" {
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.migrator.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.nightly_sync.arn
}
