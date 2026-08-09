output "lambda_function_name" {
  value = aws_lambda_function.migrator.function_name
}

output "manifest_table_name" {
  value = aws_dynamodb_table.migration_manifest.name
}

output "secrets_to_populate" {
  description = "Secret ARNs that must be populated out-of-band before first run"
  value = {
    okta_api_token         = aws_secretsmanager_secret.okta_api_token.arn
    box_jwt_config          = aws_secretsmanager_secret.box_jwt_config.arn
    google_service_account  = aws_secretsmanager_secret.google_service_account.arn
  }
}
