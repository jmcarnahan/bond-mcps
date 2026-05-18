# null_resource preconditions: cluster reachable + ALB controller Ready + every SM secret
# has a non-empty version. Refuses to proceed to services.tf if seeding was skipped.
