# Terraform: immune-api infrastructure

Creates: the guarded Lambda function, an HTTP API in front of it, a DynamoDB table for shared
blocks (TTL-cleaned), an SNS topic for alerts, CloudWatch log groups + two alarms, and a
least-privilege IAM role (the function may only touch its own table and topic).

## Use

```bash
../scripts/build_lambda.sh                 # packages code + model.json into ../build/app.zip
terraform init
cp terraform.tfvars.example terraform.tfvars   # edit it: at least set alert_email
terraform plan
terraform apply
```

`lambda_zip_path` (default `../build/app.zip`) must exist before `apply` - that's what
`scripts/build_lambda.sh` and the CI/CD pipeline (`.github/workflows/deploy.yml`) build first.

Start with `guard_mode = "monitor"`: nothing gets blocked, but every source the model would have
blocked still shows up in CloudWatch Logs (`{"event": "would_block", ...}`) and increments the
`ImmuneGuard/Blocks` metric with dimension `Mode=monitor`. Once you like what you see, set
`guard_mode = "enforce"` and re-apply.

## Destroy

```bash
terraform destroy
```
