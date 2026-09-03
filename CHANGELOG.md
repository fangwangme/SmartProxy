# Changelog

## Unreleased — PR #22: Restore adaptive feedback scoring, add circuit breaker, switch to softmax

This change closes [Issue #21](https://github.com/fangwangme/SmartProxy/issues/21).

### Scoring and Selection

- **Softmax selection strategy**: Defaults to `selection_strategy = softmax` with `softmax_temperature = 14.0`, providing smooth exponential discrimination (~30:1 for high-performing nodes vs mediocre ones) without hard cutoff truncation risks.
- **Calibrated Beta prior**: Weakened to `elo_prior_successes = 0.25` and `elo_prior_failures = 0.75` (strength 1.0, center 0.25) to align with actual baseline success rate (~10%–25%).
- **Calibrated latency band**: Updated to `latency_full_score_ms = 15000` and `latency_zero_score_ms = 60000` to reflect real target website response times via free proxies.
- **Recency Circuit Breaker**: Introduced `elo_circuit_breaker_multiplier = 0.15` and dynamic cap below the baseline (`min(penalized, baseline - 1.0)`) when the last 10 requests all fail, guaranteeing immediate ejection on the next sync cycle.
- **Pure-failure suppression**: Proxies with 0 successes and >= 2 failures are capped at `baseline * 0.5`, guaranteeing `40% success >> untried baseline >> 2+ consecutive failures` under any pool median.
- **Historical counter cold-start fix**: All-failure historical counter records from DB reseeding are scaled below baseline instead of receiving a legacy 10.0 floor.
- **Exploration ratio**: Recommended default raised to `exploration_ratio = 0.15` to ensure steady traffic for newly discovered proxies.

### Upgrade and Migration Guide

Existing deployments upgrading should update `config/config.ini` under `[source_pool]`:
```ini
[source_pool]
selection_strategy = softmax
softmax_temperature = 14.0
exploration_ratio = 0.15
latency_full_score_ms = 15000
latency_zero_score_ms = 60000
elo_prior_successes = 0.25
elo_prior_failures = 0.75
elo_new_proxy_consistency_bonus = 0.0
elo_circuit_breaker_multiplier = 0.15
```
After modifying configuration, restart the service or call `POST /reload-sources` to apply tunables immediately.

## 3.3.3 — 2026-09-02 — PR #20: Dashboard interval K-line alignment, Today quick jump, and time window selection

This change closes [Issue #19](https://github.com/fangwangme/SmartProxy/issues/19).

### Dashboard & API

- **Aggregation intervals alignment**: Updated API and frontend to standard K-line intervals `[1, 2, 5, 15, 60]` minutes (`src/api/server.py`, `dashboard/src/types/api.ts`).
- **"Today" quick-jump action**: Added a "Today" button next to Date picker in `Controls.tsx`. Clicking jumps to `today` and resumes auto-refresh; disabled when already on today.
- **Time window selection**: Added `1h`, `2h`, `5h`, `24h` window selector in `Controls.tsx`, filtering visible timeseries rows in `App.tsx` when viewing today.
- **Adaptive X-axis ticks**: Dynamically computes tick marks in `Charts.tsx` based on the visible duration and interval (e.g., 10–15m for 1h, 15–30m for 2h, 30–60m for 5h, 2h for 24h).

## 3.3.2 — 2026-09-02 — PR #18: Restore proxy supply and recalibrate scoring

This change closes [Issue #17](https://github.com/fangwangme/SmartProxy/issues/17).

### Fetcher and supply

- Proxy list downloads now run via `curl` subprocess with `--retry`, `--retry-delay`, and `--retry-connrefused`.
- HTTP status codes are extracted from curl via `--write-out "\n%{http_code}"` to classify failures:
  - Transient failures (connection resets, timeouts, 429/500/502/503/504) back off up to `backoff_transient_max_s` (300s).
  - Persistent failures (HTTP 404, invalid URL) back off up to `backoff_max_s` (1800s).
- Calling `POST /reload-sources` or restarting the service immediately resets fetcher backoffs.

### Scoring and calibration

- Recalibrated latency score range (`latency_full_score_ms = 5000`, `latency_zero_score_ms = 30000`) to match real-world free proxy latencies (8–33s), restoring the 30-point latency discriminator.
- Dynamic median baseline: unmeasured proxies score at the median score of currently measured live proxies rather than a fixed 50.0, resolving rank inversions.
- Calibration reference table updated in code docstrings, `docs/specs/proxy-quality-scoring.md`, and `README.md`.

### Reputation persistence

- Added `feedback_success_count`, `feedback_failure_count`, and `feedback_last_ts` columns to the `proxies` table to persist historical performance across in-memory stats pool eviction.
- Periodic and shutdown flushes are serialized via `feedback_persist_lock` to ensure write-back order consistency.
- Re-seeded proxies recover their historical records and decay toward the baseline over time (`elo_decay_half_life_hours = 24`, `elo_max_result_age_hours = 48`).

### Upgrade and Migration Guide

Existing deployments upgrading to 3.4.0 should perform the following steps:

1. **Database Migration**:
   Apply the non-destructive migration script to add feedback history columns:
   ```bash
   psql -U your_user -d your_db -f config/migrations/20260902_add_proxy_feedback_history.sql
   ```

2. **Configuration Updates**:
   Update `config/config.ini` to align with the new latency calibration thresholds:
   ```ini
   [source_pool]
   latency_full_score_ms = 5000
   latency_zero_score_ms = 30000
   ```
   Ensure the `[fetcher]` section contains the retry and backoff tunables if customized:
   ```ini
   [fetcher]
   connect_timeout_s = 30
   total_timeout_s = 60
   curl_retries = 2
   curl_retry_delay_s = 1
   backoff_base_s = 30
   backoff_max_s = 1800
   backoff_transient_max_s = 300
   ```

3. **Service Restart / Reload**:
   Restart the service or call `POST /reload-sources` to apply configuration and reset fetcher backoffs:
   ```bash
   curl -X POST http://127.0.0.1:8000/reload-sources
   ```
   Historical stats do not need to be cleared; in-memory scores will automatically rescore during the first pool sync.

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
