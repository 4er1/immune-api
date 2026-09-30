locals {
  name = "${var.project_name}-${var.environment}"
  tags = merge({ Project = var.project_name, Environment = var.environment, ManagedBy = "terraform" }, var.tags)
}

# ---------------------------------------------------------------------------------------------
# Shared block store: one item per blocked source, with a TTL so entries clean themselves up.
# ---------------------------------------------------------------------------------------------
resource "aws_dynamodb_table" "blocks" {
  name         = "${local.name}-blocks"
  billing_mode = var.dynamodb_billing_mode
  hash_key     = "pk"

  attribute {
    name = "pk"
    type = "S"
  }

  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }

  point_in_time_recovery {
    enabled = true
  }

  tags = local.tags
}

# ---------------------------------------------------------------------------------------------
# Alerting: every block/would-block also publishes here (see immune/guard.py Guard._alert).
# ---------------------------------------------------------------------------------------------
resource "aws_sns_topic" "alerts" {
  name = "${local.name}-alerts"
  tags = local.tags
}

resource "aws_sns_topic_subscription" "email" {
  count     = var.alert_email == "" ? 0 : 1
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

# ---------------------------------------------------------------------------------------------
# IAM - least privilege: this function may only touch its own table and topic, nothing else.
# ---------------------------------------------------------------------------------------------
data "aws_iam_policy_document" "assume_lambda" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lambda" {
  name               = "${local.name}-lambda-role"
  assume_role_policy = data.aws_iam_policy_document.assume_lambda.json
  tags               = local.tags
}

data "aws_iam_policy_document" "lambda_permissions" {
  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["arn:aws:logs:*:*:*"]
  }

  statement {
    sid       = "BlockStore"
    actions   = ["dynamodb:GetItem", "dynamodb:PutItem"]
    resources = [aws_dynamodb_table.blocks.arn]
  }

  statement {
    sid       = "Alerts"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.alerts.arn]
  }
}

resource "aws_iam_role_policy" "lambda" {
  name   = "${local.name}-lambda-policy"
  role   = aws_iam_role.lambda.id
  policy = data.aws_iam_policy_document.lambda_permissions.json
}

# ---------------------------------------------------------------------------------------------
# The guarded API function. Code + model.json are pre-packaged by scripts/build_lambda.sh so
# that `terraform apply` never has to run pip or the trainer itself.
# ---------------------------------------------------------------------------------------------
resource "aws_lambda_function" "api" {
  function_name    = "${local.name}-api"
  role             = aws_iam_role.lambda.arn
  handler          = "handler.handler"
  runtime          = "python3.12"
  filename         = var.lambda_zip_path
  source_code_hash = filebase64sha256(var.lambda_zip_path)
  memory_size      = var.lambda_memory_mb
  timeout          = var.lambda_timeout_seconds

  environment {
    variables = {
      IMMUNE_MODEL_PATH        = "model.json"
      IMMUNE_TABLE             = aws_dynamodb_table.blocks.name
      IMMUNE_SNS_TOPIC_ARN     = aws_sns_topic.alerts.arn
      IMMUNE_MODE              = var.guard_mode
      IMMUNE_MIN_EVENTS        = tostring(var.guard_min_events)
      IMMUNE_CONFIRM_SECONDS   = tostring(var.guard_confirm_seconds)
      IMMUNE_CONFIRM_EVALS     = tostring(var.guard_confirm_evals)
      IMMUNE_BLOCK_BASE        = tostring(var.guard_block_base_seconds)
      IMMUNE_BLOCK_MAX         = tostring(var.guard_block_max_seconds)
    }
  }

  tags = local.tags
}

resource "aws_cloudwatch_log_group" "api" {
  name              = "/aws/lambda/${aws_lambda_function.api.function_name}"
  retention_in_days = var.log_retention_days
  tags              = local.tags
}

# ---------------------------------------------------------------------------------------------
# API Gateway HTTP API - a thin, cheap front door. Its own throttle is a hard backstop that
# exists independently of the guard (defence in depth: the guard can fail open, this cannot).
# ---------------------------------------------------------------------------------------------
resource "aws_apigatewayv2_api" "this" {
  name          = "${local.name}-api"
  protocol_type = "HTTP"
  tags          = local.tags
}

resource "aws_apigatewayv2_integration" "lambda" {
  api_id                 = aws_apigatewayv2_api.this.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.api.invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "proxy" {
  api_id    = aws_apigatewayv2_api.this.id
  route_key = "ANY /{proxy+}"
  target    = "integrations/${aws_apigatewayv2_integration.lambda.id}"
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.this.id
  name        = "$default"
  auto_deploy = true

  default_route_settings {
    throttling_burst_limit = var.throttle_burst_limit
    throttling_rate_limit  = var.throttle_rate_limit
  }

  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.access.arn
    format = jsonencode({
      requestId = "$context.requestId", ip = "$context.identity.sourceIp",
      routeKey = "$context.routeKey", status = "$context.status",
      responseLength = "$context.responseLength", integrationError = "$context.integrationErrorMessage"
    })
  }

  tags = local.tags
}

resource "aws_cloudwatch_log_group" "access" {
  name              = "/aws/apigateway/${local.name}"
  retention_in_days = var.log_retention_days
  tags              = local.tags
}

resource "aws_lambda_permission" "apigw" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.api.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.this.execution_arn}/*/*"
}

# ---------------------------------------------------------------------------------------------
# Alarms on the guard's own EMF metrics (namespace "ImmuneGuard", see immune/guard.py).
# ---------------------------------------------------------------------------------------------
resource "aws_cloudwatch_metric_alarm" "blocking_a_lot" {
  alarm_name          = "${local.name}-many-blocks"
  namespace           = "ImmuneGuard"
  metric_name         = "Blocks"
  dimensions          = { Mode = var.guard_mode }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 20
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  tags                = local.tags
}

resource "aws_cloudwatch_metric_alarm" "lambda_errors" {
  alarm_name          = "${local.name}-lambda-errors"
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = aws_lambda_function.api.function_name }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 5
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  tags                = local.tags
}
