# Remote state: S3 + DynamoDB locking. Migrated from the local backend on
# 2026-08-31 so the cluster's source of truth no longer lives on one laptop.
# The bucket is versioned and KMS-encrypted; every state revision is
# recoverable from S3 version history.
terraform {
  backend "s3" {
    bucket         = "bond-mcps-tfstate-119684128788"
    key            = "bond-mcps/existing-vpc/dev/terraform.tfstate"
    region         = "us-west-2"
    dynamodb_table = "bond-mcps-tfstate-lock"
    encrypt        = true
  }
}
