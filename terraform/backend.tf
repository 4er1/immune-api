# Remote state lives in S3 so `terraform apply` is safe to run from CI (no local state file to lose
# or collide on). The bucket/key/region are supplied at `terraform init` time (see
# .github/workflows/deploy.yml and "Bootstrapping remote state" in the top-level README), not
# hardcoded here, so the same config works for any environment/account.
terraform {
  backend "s3" {}
}
