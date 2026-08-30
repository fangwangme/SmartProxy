# Changelog

## 3.3.1 — 2026-08-31 — PR #16: Dashboard rebuild and stats null semantics

This change closes [Issue #15](https://github.com/fangwangme/SmartProxy/issues/15).

### API behavior

- `GET /api/stats/timeseries` and `GET /api/stats/overview` report
  `success_rate: null` for a time slot with no traffic, instead of `0`.
  `total_requests` and `success_count` stay `0`, and a slot that recorded only
  failures still reports `0.0` — distinct from "no traffic". Both endpoints emit
  every slot of the day, so the previous encoding made the dashboard line drop
  to the floor and run flat from the current moment to 23:59. The dashboard is
  the only consumer of these two endpoints; no other route changed.

### Dashboard

- Rebuilt on a Gruvbox light/dark palette. The theme follows
  `prefers-color-scheme`, can be overridden, is remembered in `localStorage`,
  and is applied before first paint. `dashboard/src/theme/gruvbox.ts` is the
  only place colour values are defined; `tailwind.config.ts` reads it to emit
  the CSS custom properties and to map every Tailwind colour utility onto them,
  and the charts read the same module. Surfaces separate with borders rather
  than shadows.
- Success rate and request volume share one chart again, with one hue per
  source, a solid stroke for success rate and a dashed stroke for volume. The
  legend carries one entry per source rather than one per line, and the volume
  axis keeps its band below the success-rate lines. Distinct colours are
  available for 21 sources before any repeat.
- Chart lines break across intervals with no traffic instead of dropping to
  zero.
- The frontend is TypeScript. `bun run typecheck` and `bun run lint` are wired
  up, and `bun run build` typechecks before building. The build output stays at
  `.local/dist`, which Flask serves.
- The Search button is gone; data loads on selection and a refresh control
  triggers a manual reload. Auto-refresh no longer polls a past date or a hidden
  tab, catches up once when the tab returns, follows the day across midnight,
  and runs off a single timer instead of two.
- A failed reload clears the previous numbers instead of showing them under the
  newly selected date or source. Per-source daily totals and time series are
  committed together, so the KPI row and the chart cannot disagree about which
  day they show.
- Arrow keys step the date only when no control is focused. The interval and
  theme selectors implement standard radio-group keyboard behaviour, and the
  date field shows a focus ring again.
- A date typed past today is clamped to today, so the "next day" control and
  the auto-refresh state cannot disagree with what is displayed.
- Dismissing the error banner no longer makes the chart claim the day had no
  traffic when the request in fact failed.
- Compact KPI row, responsive toolbar, skeleton loading states, focus-visible
  styling, and a recoverable error boundary. Vite template leftovers were
  removed and `dashboard/README.md` was rewritten.

### Documentation

- `AGENTS.md` no longer claims development happens directly on `main`, which
  contradicted every merged pull request, and gains a `## Workflow` section
  covering the branch, worktree, and pull-request flow.

## 3.3.0 — 2026-08-30 — PR #14: Proxy pool quality fixes

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
