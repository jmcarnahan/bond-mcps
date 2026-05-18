# Secrets Manager SHELLS only (values seeded out-of-band):
#   - bond-mcps-${env}-encryption-key
#   - bond-mcps-${env}-db-credentials
#   - bond-mcps-${env}-<svc>-oauth (for_each services where oauth_secret_name != null)
# Every secret_version has lifecycle { ignore_changes = [secret_string] }.
