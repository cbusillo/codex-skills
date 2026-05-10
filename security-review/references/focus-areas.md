# Security Review Focus Areas

Use these focus areas to guide security reviews and threat models for specific
technologies and project types.

## Odoo Applications

- **Access Control**: ACLs, record rules, group checks, and multi-company/tenant
  boundaries.
- **Elevation**: Usage of `sudo`, `with_user`, and context flags that bypass
  security.
- **Surface Area**: Public controllers/routes, portal flows, JSON endpoints, and
  file handling.
- **Sensitive Data**: Secrets in `ir.config_parameter`, env files, logs,
  fixtures, and migrations.

## Integration & Sync Loops

- **Webhooks**: Validation of signatures, replay protection, and idempotency
  for Shopify, RepairShopr, Fishbowl, or other integration platforms.
- **Mapping**: Data consistency and tenant isolation during cross-platform sync.

## Next.js / Web Applications

- **Auth Boundaries**: Password reset, account deletion, and owner/admin paths.
- **Database Safety**: Prisma/ORM query authorization and public endpoint
  exposure.
- **Billing**: Stripe or payment webhooks, raw-body validation, and customer
  mapping.
- **IDOR**: QR codes or public resource identifiers and enumeration risk.

## Launchplane / Control Planes

- **Authority**: Secret records, runtime environment authority, and promotion
  logic.
- **Resilience**: Backup gates, restore flows, and fail-closed behavior.
- **Audit**: Auditability of sensitive changes to secrets or deployment
  pipelines.

## Infrastructure

- **Docker**: Build secrets, layer leakage, source injection, and base image
  trust.
- **Packages**: Dependency pinning, lockfile integrity, and live-credential
  leakage in tests.
