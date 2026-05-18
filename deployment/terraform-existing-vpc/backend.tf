# Local backend by default. Once multiple operators apply this stack, switch
# to S3 + DynamoDB by uncommenting the s3 block and removing the local one.
terraform {
  backend "local" {}

  # backend "s3" {
  #   bucket         = "bond-mcps-tfstate-<account-id>"
  #   key            = "bond-mcps/existing-vpc/dev/terraform.tfstate"
  #   region         = "us-west-2"
  #   dynamodb_table = "bond-mcps-tfstate-lock"
  #   encrypt        = true
  # }
}
