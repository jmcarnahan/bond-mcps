# Wildcard ACM cert for *.<base_domain>, DNS-validated via the hosted zone.
# Lives in the same region as the ALB it will serve (3b). create_before_destroy
# so a re-issue doesn't briefly leave services without a cert.

resource "aws_acm_certificate" "wildcard" {
  domain_name       = "*.${var.base_domain}"
  validation_method = "DNS"

  tags = { Name = "*.${var.base_domain}" }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_route53_record" "cert_validation" {
  for_each = {
    for dvo in aws_acm_certificate.wildcard.domain_validation_options : dvo.domain_name => {
      name   = dvo.resource_record_name
      record = dvo.resource_record_value
      type   = dvo.resource_record_type
    }
  }

  zone_id         = data.aws_route53_zone.main.zone_id
  name            = each.value.name
  type            = each.value.type
  records         = [each.value.record]
  ttl             = 60
  allow_overwrite = true
}

resource "aws_acm_certificate_validation" "wildcard" {
  certificate_arn         = aws_acm_certificate.wildcard.arn
  validation_record_fqdns = [for r in aws_route53_record.cert_validation : r.fqdn]
}
