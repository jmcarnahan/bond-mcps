# Aurora Postgres Serverless v2 cluster for the bond-mcps encrypted token DB.
#
# Notes:
# - One instance is enough for v1. Add a reader by appending another aws_rds_cluster_instance.
# - Cluster parameter group enforces TLS (rds.force_ssl=1); auth/db/session.py
#   refuses Postgres URLs without strict sslmode, so this aligns with the app.
# - master_password is a 32-char random string with no specials. Specials would
#   break the BOND_MCPS_DB_URL the chart assembles via ESO templating.
# - lifecycle ignores master_password so rotation managed outside TF (Secrets
#   Manager managed rotation, manual rotate, etc.) doesn't bounce the cluster.

resource "aws_db_subnet_group" "aurora" {
  name       = "${local.name_prefix}-aurora"
  subnet_ids = local.private_subnet_ids
  tags       = { Name = "${local.name_prefix}-aurora" }
}

resource "aws_rds_cluster_parameter_group" "aurora" {
  name        = "${local.name_prefix}-aurora-pg15"
  family      = "aurora-postgresql15"
  description = "${local.name_prefix} — force TLS for bond-mcps Aurora cluster"

  parameter {
    name  = "rds.force_ssl"
    value = "1"
  }
}

resource "random_password" "aurora_master" {
  length  = 32
  special = false # avoid URL-encoding pain in BOND_MCPS_DB_URL
}

resource "aws_rds_cluster" "bond_mcps" {
  cluster_identifier = "${local.name_prefix}-aurora"
  engine             = "aurora-postgresql"
  engine_version     = var.aurora_engine_version
  engine_mode        = "provisioned" # required for Serverless v2
  database_name      = "bondmcps"
  master_username    = "bondmcps_admin"
  master_password    = random_password.aurora_master.result

  db_subnet_group_name            = aws_db_subnet_group.aurora.name
  vpc_security_group_ids          = [aws_security_group.aurora.id]
  db_cluster_parameter_group_name = aws_rds_cluster_parameter_group.aurora.name

  storage_encrypted = true
  kms_key_id        = aws_kms_key.aurora.arn

  backup_retention_period = 7
  preferred_backup_window = "03:00-04:00"

  deletion_protection       = var.aurora_deletion_protection
  skip_final_snapshot       = !var.aurora_deletion_protection
  final_snapshot_identifier = var.aurora_deletion_protection ? "${local.name_prefix}-aurora-final" : null

  serverlessv2_scaling_configuration {
    min_capacity = var.aurora_min_capacity
    max_capacity = var.aurora_max_capacity
  }

  tags = { Name = "${local.name_prefix}-aurora" }

  lifecycle {
    ignore_changes = [master_password]
  }
}

resource "aws_rds_cluster_instance" "bond_mcps" {
  cluster_identifier = aws_rds_cluster.bond_mcps.id
  instance_class     = "db.serverless"
  engine             = aws_rds_cluster.bond_mcps.engine
  engine_version     = aws_rds_cluster.bond_mcps.engine_version

  publicly_accessible             = false
  performance_insights_enabled    = true
  performance_insights_kms_key_id = aws_kms_key.aurora.arn

  tags = { Name = "${local.name_prefix}-aurora-1" }
}
