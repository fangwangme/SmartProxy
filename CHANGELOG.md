# Changelog

## 2026-08-30 — PR #14: Proxy pool quality fixes

This change closes [Issue #13](https://github.com/fangwangme/SmartProxy/issues/13).

### Runtime behavior

- Proxy selection now excludes proxies that failed the latest validation while
  retaining their feedback history for later recovery.
- Feedback scoring now uses a small-sample prior, reliability-adjusted latency,
  time decay, periodic full-pool rescoring, and a configurable exploration
  budget. The default exploration ratio is 5%.
- Live proxy reputation is no longer discarded by stats-pool truncation; the
  configured cap applies to retained dead-proxy history.
- Feedback and restored backup values are normalized before scoring. Backup
  writes are atomic, and restore commits only after the complete snapshot has
  passed structural validation.
- Source-list caching now preserves the last known good value across transient
  database failures.

### Configuration and tooling

- Python 3.14 and uv are now the primary environment. `uv.lock` is authoritative;
  `requirements.txt` remains as the generated pip-compatible fallback.
- New or newly documented settings include `selection_strategy`,
  `proxy_cooldown_ms`, `exploration_ratio`, `validation_new_proxy_ratio`,
  `elo_prior_successes`, `elo_prior_failures`, `rescore_on_sync_enabled`,
  `elo_max_result_age_hours`, and `max_feedback_latency_ms`.
- `config/config.ini` remains git-ignored and is not rewritten by this change.
  The recommended backup path is `.local/data/proxy_stats_backup.json`; missing
  settings use code defaults and are reported by the config-drift check.
- `/reload-sources` now reloads all runtime tunables transactionally. Database,
  server-port, and logging changes still require a restart.

### Database

- Stats queries use half-open timestamp ranges and the composite
  `(source_name, minute)` index.
- New databases receive the index through `config/database_setup.sql`.
- Existing databases can apply
  `config/migrations/20260830_add_source_stats_source_minute_index.sql`; the
  migration is non-destructive, concurrent, and idempotent. The setup script is
  destructive and must not be used as an upgrade script.
