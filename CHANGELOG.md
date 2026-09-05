# Changelog

## 3.3.6 — 2026-09-05 — PR #26: Reliability contracts, lifecycle, and observability

This change closes [Issue #25](https://github.com/fangwangme/SmartProxy/issues/25).

3.3.5 reached a working steady state; this release is about what happens at the
edges of it — a validation target going down, a database stalling, a restart, a
shutdown, a malformed forwarded header. The scoring model is unchanged.

### Validation and configuration

- **A target outage no longer empties the pool.** Validation results are now
  tracked per target, and a batch only commits deactivations when a healthy
  quorum of targets answered. Without it, existing active proxies keep their
  last-known-good liveness. Never-validated candidates in a failed batch are
  recorded as failed so one bad oldest batch cannot starve newer discoveries;
  the ordinary failed-proxy retry window reconsiders them after recovery.
  `validation_target_min_samples` sets how much evidence marks a target
  reachable.
- **Out-of-range configuration is rejected, not clamped.** Every tunable is
  semantically validated — worker counts, timeouts, intervals, percentages,
  pool bounds and cross-field relationships — at startup and before a reload
  mutates any active value. A value that used to be silently pulled to a
  boundary now fails loudly.
- **Fetch backoff survives a restart.** Failure count, class and next-attempt
  time are persisted in `proxy_source_fetch_state`, so restarting no longer
  re-hammers a source whose circuit is still open.
- Three `[proxy_source_*]` endpoints that are permanently unavailable were
  dropped from the example config.

### Handout, feedback, and premium

- **`/get-proxy` and `/get-premium-proxy` return `source`.** For a premium
  proxy this is the only way a client can know which pool the result should be
  scored against; it previously had to guess.
- **Premium routes through the normal contract.** It was a `random.choice()`
  over a list refreshed only on sync, so a proxy that had already failed its
  way out could still be handed out. It now honours source, liveness,
  qualification, eligibility, the outstanding-handout lease, and immediate
  demotion.
- **Feedback with no outstanding handout is counted**, not rejected. The
  service records what the client reports and the client owns whether that
  report is right; `smartproxy_feedback_unmatched_total` is what a duplicate, a
  late report or a wrong `source` looks like from here. Leases do not survive a
  restart, so a restart adds roughly one count per in-flight request, and a
  report later than `proxy_inflight_timeout_seconds` counts too.
- **Latency no longer orders the pool.** It was a secondary sort key for tied
  scores; measurement showed the tie it served does not occur — across 8000
  stored stats only two groups shared a score, neither with distinct latencies.
  `avg_latency_ms` is now recorded and nothing else.

### Persistence and lifecycle

- **Reputation is no longer mirrored into PostgreSQL.** The
  `proxies.feedback_success_count` / `feedback_failure_count` /
  `feedback_last_ts` columns are gone, along with the hydration and double-write
  path. The JSON backup is the only durable copy: if it is absent, proxies
  restart from the fixed prior and relearn.
- **Minute aggregates are acknowledged after commit.** A `feedback_flush_commits`
  ledger makes a retried flush idempotent, failed writes stay queued instead of
  being dropped, writers touching overlapping rows use a deterministic order,
  and retries are bounded and limited to retryable PostgreSQL SQLSTATEs
  including deadlock.
- **Shutdown is deadline-driven.** `shutdown_deadline_seconds` bounds stopping
  the scheduler, draining tracked work, flushing the current partial minute and
  writing the final backup; the launcher waits that long plus a small margin.
  The backup goes first: a stalled database is exactly the case where a local
  file can still be written, and a missed backup costs a relearning period
  while a missed minute is re-sent by the ledger.
- **The schema is authoritative.** `config/database_setup.sql` is the only
  definition and there are no migration scripts; an existing database is
  upgraded by running the same file again.
- `--fresh-scoring` is removed. `--no-restore` remains.

### Runtime, API, and observability

- **Production serves through one Waitress process** with `production_threads`,
  because allocation, lease and scoring state are process-local. `--debug`
  keeps Flask's development server. Do not add WSGI worker processes.
- **`/live` and `/ready` are added**, and `/health` no longer reports a failed
  dependency as healthy: it returns `503` with `status: degraded` when the
  database, scheduler, recent validation quorum, recent flush, or minimum
  usable pool is not satisfied.
- **Statistics endpoints validate before querying** and return `503` on a
  backend failure instead of a successful all-zero payload.
- **Prometheus counters have counter semantics.** They increment once per
  accepted feedback rather than being summed from retained pool state, where a
  dead-proxy fan-out or an eviction could double or decrease them.
- **Forwarded headers fail closed.** The client is derived from the trusted
  proxy boundary rather than the left-most value, a malformed chain is refused
  rather than falling back to the direct peer, and loopback detection covers
  IPv4-mapped addresses.
- **`X-Server-Time` is a full timezone-aware timestamp.** The dashboard
  advances that clock locally and derives "today" and its moving windows from
  it instead of the viewer's calendar.
- Request threads never synchronously rebuild an expired serving plan under the
  manager lock; the last immutable plan is served while one background refresh
  per source runs.
- Persistent log sinks are initialised only by the process entry point, so an
  import or a test cannot write into operational logs. **Exception logs no
  longer include local variable values** (`diagnose=False`): the variables in
  scope on this path can carry proxy addresses. Stack traces are unaffected.
- The launcher reads the port from the config, validates PID ownership before
  signalling (and again after the pre-stop backup, in case the PID was reused),
  and uses a unique temporary response file.

### Metrics changes

Removed: `smartproxy_requests_success_total`, `smartproxy_requests_failure_total`.

Added: `smartproxy_feedback_accepted_total{outcome="success"|"failure"}`,
`smartproxy_feedback_unmatched_total`,
`smartproxy_validation_target_failures_total{target_index,kind}`,
`smartproxy_backup_duration_seconds`, `smartproxy_manager_lock_hold_seconds`,
`smartproxy_plan_refresh_duration_seconds`.

`smartproxy_success_rate_percent` changes meaning: it is now derived from
process-lifetime accepted-feedback counters and therefore resets on restart,
rather than being summed from the retained stats pool.

### Upgrade and Migration Guide

Order matters: the JSON backup is now the only durable copy of proxy
reputation, so stop through the launcher, which backs up first.

1. **Stop the running service** with `./scripts/start_proxy.sh stop`. Remove
   `--fresh-scoring` from any launcher invocation, service unit, or cron entry.

2. **Install the new dependency** (Waitress 3.0.2):

   ```bash
   uv sync --locked
   ```

3. **Rebuild the schema.** There are no migrations; rerunning the file is the
   upgrade path. It drops stored proxies and minute aggregates — proxies are
   rediscovered by the fetchers within one source-refresh interval, and
   reputation is in the JSON backup, but **dump `source_stats_by_minute` first
   if you need the historical dashboard series**:

   ```bash
   psql -U your_user -d your_db -f config/database_setup.sql
   ```

4. **Add the new keys to `config.ini`** (shown at their defaults). None are
   required to start — all fall back to these values — but `check_config_drift()`
   reports each one as missing at startup:

   ```ini
   [server]
   production_threads = 8
   background_workers = 8
   shutdown_deadline_seconds = 20
   readiness_min_usable_pool = 1
   readiness_validation_max_age_seconds = 600
   readiness_flush_max_age_seconds = 180

   [database]
   min_connections = 2
   write_max_retries = 3
   write_retry_base_ms = 25

   [validator]
   validation_target_min_samples = 1
   ```

   Size `shutdown_deadline_seconds` above the observed drain and flush time plus
   `smartproxy_backup_duration_seconds`, rather than assuming 20s fits this host.

   No keys were removed in this release. Three `[proxy_source_*]` sections were
   dropped from the example config for being permanently unavailable
   (`https_list_F`, `http_list_O`, `http_list_P`); remove them from your own
   config if present.

5. **Rebuild the dashboard.** The server clock header changed format, and a
   stale bundle cannot parse it — "today" and the moving windows silently stop
   tracking the server:

   ```bash
   cd dashboard && bun install && bun run build
   ```

6. **Update monitoring** for the metric changes above, and point liveness at
   `/live` and readiness at `/ready`. A probe that treats any non-200 from
   `/health` as an outage will now fire whenever a dependency degrades, which
   is the intent — but it is a new alerting surface.

7. **Start the service** and confirm `/ready` reports all five dependencies
   healthy.

## 3.3.5 — 2026-09-03 — PR #24: Online proxy learning, control-plane routing

This change closes [Issue #23](https://github.com/fangwangme/SmartProxy/issues/23).

The composite ELO score over a sliding window is replaced by two online
reliability estimators, and routing is split into a control plane and a data
plane. Thresholds throughout are calibrated against the deployment's own
feedback record rather than against a pool that mostly succeeds.

### Scoring

- **Two-speed online reliability**: each proxy carries independent per-source
  `quality_slow` and `quality_fast` estimators, both starting at a fixed
  `reliability_prior` (5%), with `score = 100 * min(slow, fast)`. The slow
  estimator (`reliability_slow_alpha = 0.12`) limits one-hit promotion; the fast
  one (`reliability_fast_alpha = 0.30`) makes deterioration visible immediately.
- **Fixed prior, never a population median**: the old untried baseline was the
  median of the measured pool, which collapsed toward zero exactly when most of
  the pool was failing. The prior is now a constant, chosen from the observed
  distribution — about 82% of live proxies here sit below a 5% success rate.
- **Time-based forgiveness**: both estimators decay toward the prior on
  `reliability_decay_half_life_hours`, so old good and old bad state converge on
  the same untried score. Ageing cannot reverse the sign of evidence.
- **Latency leaves the score entirely**: it stays observable as `avg_latency_ms`
  and only breaks ties between equal scores.
- **Undated history is not trusted as fresh**: a durable counter record whose
  `feedback_last_ts` is missing is aged to the prior rather than seeded from its
  lifetime rate. The raw counters survive, and the proxy re-enters as a
  discovery candidate.

### Selection and trial allocation

- **Control plane / data plane split**: eligibility, qualification, ranking and
  selection weights are computed by the serving plan, inside the pass the pool
  sync already makes over the pool, and refreshed on
  `serving_plan_max_age_seconds`. `get_proxy()` holds no pool logic — one draw
  on the exploration budget, then O(1) for a trial candidate or O(log n) against
  precomputed cumulative weights for an exploit pick. Per-request cost no longer
  scales with pool size.
- **One adaptive exploration budget**: falls from `exploration_max_ratio` to
  `exploration_min_ratio` as the live pool becomes evaluated, measured against
  the larger of `exploration_target_qualified` and
  `exploration_target_qualified_ratio` of the live pool. Two thirds goes to
  never-tried discovery by default.
- **Probation and delayed retries**: `probation_attempts` immediate handouts,
  then `retry_attempts` more, each after `retry_delay_seconds`. The budget is
  spent by trial handouts only, and qualifying returns it, so a later dip below
  the prior costs probation rather than the pool slot.
- **Every live qualified proxy exploits**: `max_pool_size` and `top_tier_size`
  bound the ranked tier lists, which weight the `tiered` strategy. They are not
  eligibility gates — the tier lists are recomputed only on a pool sync, so
  gating on them stranded any proxy that qualified in between.
- **Trial claim and return**: a trial candidate is removed from the plan when
  handed out and returned when its feedback arrives, which enforces one
  outstanding request per untried proxy at no per-request cost.
  `proxy_max_inflight` is an opt-in per-proxy concurrency cap for qualified
  proxies, default 0 (unlimited); when set, it is enforced on the draw.
- **`proxy_cooldown_ms` now spaces out trial handouts only.** Applying it to
  exploitation would drop the busiest — and therefore highest-scoring — proxies
  from every plan rebuild.

### Source outage guard

- A source-wide outage pauses per-proxy reputation mutation after a healthy
  completed window, rolls back the triggering window's tentative changes, and
  resumes on a completed recovery window. Aggregate per-minute feedback
  continues in every state, and a paused source broadcasts nothing to other
  sources.
- **Thresholds are multiples of the source's own baseline**, never absolute
  ratios, and the verdict window sizes itself so an all-failure run is less
  likely than `outage_false_positive_budget` — about 66 observations at a 10%
  baseline, three at 90%. Absolute gates could not work here: none of the
  observed completed minutes reached a 50% success rate, while 90% failure is
  normal operation.
- Prometheus metrics expose active state and paused-update totals per source.

### Persistence and runtime modes

- JSON snapshots carry `scoring_version = 2`. A missing or mismatched version
  never trusts stored derived scores: valid `recent_results` are replayed in
  timestamp order. Raw feedback and database counters are preserved.
- Durable database counters are written monotonically, serialized in-process,
  and failed batches are re-queued.
- `--no-restore` skips the JSON restore but keeps database hydration;
  `--fresh-scoring` skips both and disables durable reputation writes. Each
  writes to its own sibling snapshot path, so neither touches normal state. The
  launcher is renamed to `scripts/start_proxy.sh` and forwards both flags.

### Upgrade and Migration Guide

`[source_pool]` is replaced wholesale. Remove these keys — they are no longer
read, and `check_config_drift()` will report them at startup:

```
elo_max_window, elo_scoring_window, elo_time_decay_enabled,
elo_decay_half_life_hours, elo_max_result_age_hours, elo_prior_successes,
elo_prior_failures, elo_new_proxy_consistency_bonus,
elo_circuit_breaker_multiplier, elo_baseline_floor, rescore_on_sync_enabled,
exploration_ratio, latency_full_score_ms, latency_zero_score_ms
```

Add these (shown at their defaults):

```ini
[source_pool]
# two-speed online reliability
reliability_prior = 0.05
reliability_slow_alpha = 0.12
reliability_fast_alpha = 0.30
reliability_decay_half_life_hours = 24
reliability_recent_results_limit = 100
reliability_history_prior_weight = 5

# adaptive exploration, probation and trial claim/return
exploration_min_ratio = 0.05
exploration_max_ratio = 0.30
exploration_target_qualified = 50
exploration_target_qualified_ratio = 0.5
exploration_discovery_share = 0.6666666667
qualification_min_results = 3
probation_attempts = 3
retry_attempts = 2
retry_delay_seconds = 3600
probation_forgiveness_hours = 48
proxy_inflight_timeout_seconds = 120
proxy_max_inflight = 0
exploit_draw_attempts = 4
serving_plan_max_age_seconds = 2.0

# source outage guard, relative to each source's own baseline
outage_guard_enabled = true
outage_window_size = 20
outage_window_max_size = 200
outage_min_distinct_proxies = 10
outage_false_positive_budget = 0.001
outage_baseline_alpha = 0.2
outage_healthy_baseline_ratio = 0.5
outage_failure_baseline_ratio = 0.1
outage_recovery_baseline_ratio = 0.3
```

Also set `proxy_cooldown_ms = 0`: it now applies to trial handouts only, and a
value below `serving_plan_max_age_seconds` would drop the busiest proxies from
every plan rebuild. Use `proxy_max_inflight` if per-proxy protection is needed.

Existing JSON snapshots are read without a `scoring_version` and their
`recent_results` are replayed into the new estimators, so scoring history
survives the upgrade. `scripts/start.sh` no longer exists; use
`scripts/start_proxy.sh`.

## 3.3.4 — 2026-09-03 — PR #22: Restore adaptive feedback scoring, add circuit breaker, switch to softmax

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
