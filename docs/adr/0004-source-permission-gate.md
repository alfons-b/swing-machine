# ADR 0004 — Data source registry and permission gate

- Status: Accepted
- Date: 2026-11-21

## Context
The PRD forbids sources without clear permission and forbids bypassing authentication, paywalls
or rate limits (§6.2, NFR-013). Free IDX data sources have terms that are unclear or restrictive
(open issues #1, #3, #4).

## Decision
- `config/sources.yaml` is the reviewed, version-controlled registry. `idx sources sync` copies it
  into `data_sources` / `source_permissions`. Git history is the audit trail of reviews.
- Permissions are given **per purpose**: `automated_ingestion`, `manual_import`, `raw_archive`,
  `redistribution`. A missing purpose means not permitted.
- An `approved` permission must have `reviewed_at`, `reviewed_by` and `evidence_url`; the
  schema enforces this. Permissions can expire (`valid_until`).
- `require_permission()` runs before any adapter is called. If it fails, no run is created and no
  request is sent. A source removed from the file is set to `inactive`, which cancels its
  permissions.
- Raw payloads are archived only when `raw_archive` is approved. Otherwise only the hash and
  metadata are kept.
- Third-party sources (IDX, KSEI, OJK, BI, BPS, Yahoo Finance) are registered as
  `pending_review`. **No third-party adapter is enabled.**
- `manual_csv` (files the user supplies) is approved for `manual_import` and `raw_archive`. The
  user is responsible for making sure the original provider allows personal storage and analysis
  of each file. The file's `provider` can be recorded with `--provider`.

## Consequences
- The data pipeline can be built and tested end to end now, using files the user imports.
- Enabling an automated source is a reviewed change to `sources.yaml` plus a new adapter. It is
  never just a code change.
