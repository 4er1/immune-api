output "api_url" {
  value = aws_apigatewayv2_api.this.api_endpoint
}

output "lambda_function_name" {
  value = aws_lambda_function.api.function_name
}

output "dynamodb_table_name" {
  value = aws_dynamodb_table.blocks.name
}

output "alerts_topic_arn" {
  value = aws_sns_topic.alerts.arn
}
