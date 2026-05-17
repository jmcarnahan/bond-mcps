terraform {
  required_version = ">= 1.0"

  backend "s3" {
    bucket         = "bond-ai-terraform-state-019593708315"
    key            = "mcps/atlassian/terraform.tfstate"
    region         = "us-west-2"
    dynamodb_table = "bond-ai-terraform-locks"
    encrypt        = true
    profile        = "agent-space"
  }

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    null = {
      source  = "hashicorp/null"
      version = "~> 3.1"
    }
  }
}
