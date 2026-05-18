# bond-mcps EKS Terraform module

Deploys an own EKS cluster + Aurora Postgres + N MCP services on top of an existing VPC.

See `/Users/jcarnahan/.claude/plans/staged-squishing-lynx.md` for the full design.

Sections to fill in:
- Prerequisites (VPC ID, hosted zone, AWS creds)
- First-time apply order (phased)
- Secret seeding runbook (`aws secretsmanager put-secret-value` commands)
- Add-an-MCP recipe
- Single-service deploy (toggle `enabled`)
- Teardown
