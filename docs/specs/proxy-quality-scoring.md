# Proxy Quality: Online Reliability and Candidate Pool Selection

This spec defines the scoring contract from issue #23 and the routing contract
that replaced its selection boundaries in issue #27. Implementation:
`src/core/proxy_manager.py`.

**The governing rule.** Learning decides *which* proxy is served, never
*whether* one is served. No threshold sits anywhere on the routing path, and no
routing parameter can empty the servable set at any value in its accepted
range. A parameter whose wrong value degrades performance is a tunable; a
parameter whose wrong value empties the servable set is a design defect.

## 1. Validation and feedback remain separate

Validation owns only the `is_active` liveness signal. Real client feedback owns
reliability and traffic allocation. Validator latency, anonymity, and pass/fail
observations must never enter the reliability score.

`is_active` is a *signal*, not an admission test. It breaks ties in the
candidate pool's ranking and orders its exploration rotation (section 4); a
proxy that has never been validated, or failed its last check, is still
servable.

Every completed check is recorded as measured, pass or fail, whatever the rest
of its batch did. Its timestamp and attempt count are what move a proxy to the
back of its validation queue; a batch whose results were withheld because
nothing in it passed left the same oldest rows at the head of the queue for
every following cycle, and left dead proxies marked alive.

The cost of that honesty is an unreachable validation target. Every proxy it
checked while down is recorded as failed, and a failed proxy is checked again
only when the oldest-first failed-proxy queue comes round to it - a full pass
over every inactive row, which on a table of mostly dead rows takes hours.
Until then those proxies lose their `is_active` signal, and
`/get-premium-proxy`, which requires it, has fewer or none to offer. Supply is
not affected: serving never depends on `is_active`, and proxies with good
feedback records stay in the candidate set however they were last validated
(4.1). The per-target health verdict
(`validation_target_min_samples`) is kept as a diagnostic for the log and
`/ready`. A task that ends without a verdict - cancelled, or broken in a way
the per-proxy handlers did not anticipate - is not recorded at all.

A batch reads its validation config once, when it starts: targets, success
threshold, timeout and concurrency. A reload that lands mid-batch applies from
the next one; the requests and the summary of their results always agree on
the target list.

Feedback latency remains observable as `avg_latency_ms`. It is recorded and
nothing else: it does not enter the score, and it does not order the pool
either. It was a secondary ordering key for tied scores until measurement
showed the tie it served does not occur - across 8000 stored stats only two
groups shared a score, and neither held distinct latencies - so a 1ms success
and a 30s success are now worth exactly the same to selection.

## 2. Two-speed online reliability

Each proxy has independent per-source `quality_slow` and `quality_fast`
estimators. Both start at a fixed configured prior `p0` (default `0.05`) and
update for every accepted client result:

```text
slow = (1 - slow_alpha) * slow + slow_alpha * outcome
fast = (1 - fast_alpha) * fast + fast_alpha * outcome
score = 100 * min(slow, fast)
```

`outcome` is 1 for success and 0 for failure. Defaults are
`slow_alpha = 0.12` and `fast_alpha = 0.30`. The slow estimator limits one-hit
promotion; the fast estimator makes deterioration visible immediately. Scores
remain bounded to 0-100 for API and dashboard compatibility.

The prior is fixed, never derived from the current population. With defaults:

| Evidence | Score |
| --- | ---: |
| untried | 5.00 |
| one failure | 3.50 |
| two failures | 2.45 |
| one success | 16.40 |

Changing any equal-age result from failure to success must increase the score.
Every immediate failure must lower it and every immediate success must raise it.

## 3. Time-based forgiveness

Before applying a new event and during pool sync, both estimators decay toward
`p0` using `reliability_decay_half_life_hours`. Aging cannot reverse the sign
of evidence: old good and bad state both converge on the initial score.

`recent_results` is retained as bounded raw replay data. It is not a separate
sliding-window scorer. It is normalized once on the way in - at the API
boundary, by `restore_stats()`, and by `_migrate_legacy_stat()` - and appended
in timestamp order. Counter-only history is seeded from a lifetime
success rate shrunk toward `p0`, then aged from its last feedback timestamp. A
record whose timestamp is missing or unusable is aged to the prior instead of
trusted as fresh: unknown age is unbounded age, and the score decides pool
membership and ordering. The raw counters survive either way, so the proxy
earns its score back on fresh evidence while remaining servable throughout.

## 4. Candidate pool selection

Ranking is the only mechanism. Each source keeps a fixed-size candidate pool,
rebuilt on a timer; a handout is a uniform random pick from that pool.

```text
every pool_refresh_seconds, over every proxy the router can see (4.1):

  ranked slots       candidate_pool_size - exploration_slots, by current score
  exploration slots  exploration_slots, rotated over everyone else (4.2)
```

The ranking is the current score and nothing else feedback wrote. A success
long ago buys no slot once later failures have taken the score down, and a
proxy that keeps failing yields its slot on the next rebuild. Validation only
breaks ties: in practice that orders proxies no feedback has reached yet,
where it is the best evidence available, so the live set goes before the
reserve. Among equal scores in the same place, whoever was tried longest ago
goes first, and the URL settles the rest so two rebuilds over the same state
agree - `active_proxies` is a set and iterates in no reproducible order.

Score decides who makes the cut. It never decides whether a proxy may serve.
The pool therefore always holds `min(candidate_pool_size, everything
available)`, which makes an empty answer structurally impossible while the
database holds any proxy at all. The invariant is a property of the data
structure, not a fallback branch someone has to remember to write - and it is
asserted by test, not by review note.

A cold pool is the same code path as a warm one. With nothing validated and
nothing scored, the reserve fills every slot from whatever the fetchers have
found; validation and feedback then reorder the pool over the following hours,
and the served-quality curve rises while the served volume never drops.

The score's own shape decides how long a mediocre proxy keeps its slot. With
the default prior and `score = 100 * min(slow, fast)`, a proxy that succeeds
one time in ten drops below an untried one after five or six consecutive
failures and goes back to the rotation; at low success rates that concentrates
traffic less than a rule that kept every proxy with any success on record at
the top - which is exactly what let proxies that had succeeded once and then
died hold the pool.

### 4.1 What the router can see

`get_active_proxies()` answers `is_active = true` only, so the routing layer
cannot see a never-validated proxy through it - which on a cold start is the
entire population. The reserve supplies the rest, in two bounded parts held in
memory as `reserve_proxies`, so the pool refresh stays lock-only and never
waits on the database:

1. **The first page of the table in fallback order**: `get_reserve_proxies()`,
   never-validated rows first - an unmeasured proxy is a better guess than one
   that has already failed a check - then by the stalest last check,
   `candidate_pool_size` rows. `idx_proxies_reserve_pool` matches that
   ordering so the `LIMIT` stops at the first page rather than sorting every
   dead row in the table.
2. **The rows with the best feedback records**: for each source, the
   `candidate_pool_size` best-scored records carrying feedback that neither
   the live set nor that page covers, checked against the table in one
   bounded lookup (`get_existing_proxies()`). A failed check stamps a row
   newest, which pushes it off the first page; without this part a proxy real
   traffic keeps succeeding on lost its ranking - and all of its traffic - the
   moment it missed one check. No record below that many better ones can win a
   slot anyway, and the existence check keeps history whose row is gone (a
   rebuilt database, then a restored backup) from being served.

Placement on the sync rather than the refresh timer is deliberate. Running the
queries on the timer would put a network round trip inside the routing control
plane, and a brief database outage would then empty the fallback that carries
a cold start. A failed query keeps the previous list rather than emptying it.

Publication is event-driven, and must never be conditional on a validation
cycle *succeeding* - rows are servable the moment they are committed, whatever
the validator later makes of them. Two paths publish it:

| Path | Why |
| --- | --- |
| pool sync | the ordinary case, alongside the active query; every completed validation cycle ends in one |
| a fetch that committed rows | "serves from the first fetch onward" means the fetch, not the validation cycle that may follow minutes later |

The fetch path goes through `_publish_reserve_pool()`, which republishes the
reserve and rebuilds but never reads or rewrites `active_proxies`. Each
source's rows are committed and published as soon as its own fetch returns; a
source that spends its whole curl retry budget holds back only itself.

Reserve proxies keep their records under the stats cap exactly as live ones do:
`_truncate_stats_pool()` exempts the whole servable set, live and reserve. The
pool re-seeds a blank stat for any member that lacks one, so evicting a reserve
proxy that is still being served would launder its failure history.

### 4.2 Exploration slots

`exploration_slots` of the N slots rotate over the proxies the ranking left
out. It exists because a pool filled purely by score would never try anything
below the cut again: a newcomer would wait behind every proxy scored above the
prior, and a proxy that failed and has since recovered would never get the
trial that shows it.

The rotation takes the live set first, then whoever was tried longest ago -
the later of the last handout and the last feedback, never counting as
longest. Each trial moves a proxy to the back of the line, so newcomers go
first and everything else comes round again; nothing is locked out for good,
and no failure record is cleared to make that happen. A proxy that earns a
good score in its turn keeps its place through the ranking.

It is a rotation, not a gate:

- `0` disables it and the pool still fills by score;
- a value at or above `candidate_pool_size` still fills the pool, it merely
  spends every slot on the rotation - degraded quality, never degraded
  availability.

### 4.3 What is not on the routing path

Attempt history and per-proxy concurrency may be tie-breaks or weights when
filling the pool. Neither may remove a proxy from it.

- **Leases** (`inflight`, bounded by `proxy_inflight_timeout_seconds`) are
  bookkeeping. Their only consumer is `process_feedback`, which reads the count
  to tell a first report from a duplicate or a late one. Selection never looks
  at one, so a burst against a one-proxy pool keeps being served by that proxy.
  A lease is runtime state and is not persisted: it cannot outlive the process
  that granted it, so restoring one would only manufacture a phantom.
  Expired leases are reclaimed where the list grows - `_grant_lease()` drops
  the expired prefix before appending - and again in the sync's pass over every
  stat, which is what reaches proxies that stopped being handed out entirely.
  Feedback alone is not enough: a client that takes proxies and never reports
  is precisely the case that sends none, and the list is deep-copied and
  serialised by every backup.
- **Weighting inside the pool is dormant.** The draw is uniform;
  `selection_strategy`, `softmax_temperature`, `selection_weight_floor` and
  `top_tier_load_percentage` are parsed and validated but not consulted.
  Re-introducing weights on top of the pool is a later change and an
  optimisation, not a requirement.
- **The ranked tier lists** (`max_pool_size`, `top_tier_size`) are reporting
  and the input that later weighting change would use. They are not an
  eligibility gate and are not what `/get-proxy` reads.

### 4.4 Removed in issue #27

Four absolute thresholds sat in front of this ranking system, and each could
independently empty the servable set. A deliberate cold start on 2026-09-05
produced 1,178 refusals while 300+ proxies were validated alive; the score gate
was the load-bearing one and was self-defeating, since the threshold was by
definition the score of an unmeasured proxy.

| Removed gate | Rule it enforced |
| --- | --- |
| Validation | only `is_active` proxies were candidates |
| Score | `>= qualification_min_results` results **and** `score > prior` |
| Attempts | 3 probation handouts, then a locked retry delay, then exile |
| Concurrency | unqualified proxies hardcoded to 1 in-flight request |

With them went 18 functions and 13 routing tunables:
`exploration_min_ratio`, `exploration_max_ratio`,
`exploration_target_qualified`, `exploration_target_qualified_ratio`,
`exploration_discovery_share`, `qualification_min_results`,
`probation_attempts`, `retry_attempts`, `retry_delay_seconds`,
`probation_forgiveness_hours`, `exploit_draw_attempts`, `proxy_max_inflight`
and `proxy_cooldown_ms`. `serving_plan_max_age_seconds` became
`pool_refresh_seconds`, and `avg_latency_alpha` became a module constant since
nothing reads `avg_latency_ms` to decide anything.

`proxy_cooldown_ms` went with them although it is not one of the four gates: a
cooldown filters pool members, so at a large enough value it holds every member
out at once, which the governing rule does not permit to survive.

### 4.5 Replaced: tiers by lifetime success

The first version of this pool filled three tiers in order - any successful
feedback on record, then validated, then the rest - and reserved its
exploration slots for proxies no feedback had ever reached. Both rules were
identities rather than evidence, and neither expired:

- a proxy that had succeeded once kept its place ahead of every proxy without
  a success, whatever came after: 200 proxies that succeeded once and then
  failed a hundred times each held 180 of 200 slots at a score of 0.0;
- one failure took a proxy out of exploration for good, so once every newcomer
  had been tried once, those same 200 held all 200 slots;
- a proven proxy that failed one validation check fell off the reserve page
  and lost all of its traffic.

Ranking by current score, rotating exploration, and retaining the best
feedback records (4.1) replace them. In a replay where the 20 proxies that
carried a pool died and 20 others started succeeding, the tiered pool never
recovered - it could not re-admit a proxy it had measured once - while the
ranked pool's served success rate climbed to the uniform-draw ceiling.

## 5. Source-wide outage guard

The outage guard observes source results separately from scoring. Every
threshold is a multiple of the source's own success baseline - an EMA over
completed windows that outage windows never feed - because absolute ratios do
not survive contact with a pool whose normal success rate is 10%: an absolute
"healthy window is 50% successful" gate never opens there, and an absolute
"90% failure" trigger sits below that pool's normal state.

The window sizes itself to the baseline. A verdict needs enough observations
that an all-failure run of that length is less likely than
`outage_false_positive_budget` under the baseline: about 66 observations at a
10% baseline, three at 90%, bounded by `outage_window_size` and
`outage_window_max_size`.

A uniformly poor cold start cannot arm the guard: the first completed window
defines its own reference, and a reference of zero fails every gate.
Activation requires:

1. a completed window reaching `outage_healthy_baseline_ratio` of the baseline;
2. a following completed window at or below `outage_failure_baseline_ratio` of
   it; and
3. the configured minimum number of distinct proxies in that failure window.

Tentative proxy mutations from the triggering broad-failure window are rolled
back, field by field rather than by deep-copying each proxy's full result
history on every healthy feedback event. While active, aggregate per-minute
feedback continues, in-flight leases are released, and proxy reputation
mutation pauses for every source rather than only the reporting one - a
`dead` report from a source the guard has judged unreliable must not strip the
reputation those proxies earned elsewhere. A paused source keeps serving from
its pool throughout; the guard protects reputation, never availability.
A completed recovery window reaching
`outage_recovery_baseline_ratio` of the baseline, with enough distinct
proxies, resumes learning. Transitions are logged, and
Prometheus metrics expose active state and paused-update totals per source.

## 5a. Pool refresh

Routing is split into a control plane and a data plane.

`_rebuild_candidate_pool()` decides everything: which proxies are in the pool
and in what order. It runs inside the pool sync - reusing the pass that already
refreshes every score - and on `pool_refresh_seconds` between syncs. It also
seeds a stat for every pool member, because anything that can be handed out
needs somewhere to record feedback, or a proxy served from the reserve could
never earn its score.

`allocate_proxy()` holds no pool logic: one dict lookup and one
`random.choice`. Nothing in it scales with the size of the pool, and nothing in
it can refuse. Its only `None` is an empty pool, which means an empty database.

The pool is therefore allowed to be stale, and that staleness is bounded by
`pool_refresh_seconds`. Nothing is repaired on the request thread - a rebuild
there would put pool-sized work behind the manager lock on every handout, and a
request arriving while the pool is stale still has a pool. Staleness costs
ranking accuracy, which feedback corrects on the next rebuild; it never costs
availability, which is the trade the previous design got backwards.

The one case built on demand is a source whose pool does not exist yet, which
covers embedders that construct a manager without running the sync lifecycle.

## 6. Persistence

JSON snapshots contain root-level `scoring_version = 2`. Matching-version
derived estimator state is validated and restored. Keys an older build wrote
that the current one does not know - `trial_handout_count`, `retry_after_ts`
and `inflight` among them - are ignored on load, never rejected. There is no
migration script: `database_setup.sql` is the only schema authority. A missing or mismatched
version never trusts stored derived scores: valid `recent_results` are replayed
in timestamp order.

Proxy reputation is not mirrored into PostgreSQL. If a JSON snapshot is absent
or intentionally skipped, each proxy starts from the fixed prior and relearns
through normal feedback. This accepts a bounded warm-up period in exchange for
removing reputation migrations, hydration, and a second write path.

Startup restores a snapshot only if it was generated at most
`stats_restore_max_age_hours` (default 12) ago, judged by the `timestamp` the
snapshot records - never by the file's mtime, so copying an old file does not
make it new. Timestamps are written with their UTC offset; one without an
offset is read as local time, which is how earlier backups wrote it. A missing
or unreadable timestamp, or an older snapshot, skips the restore and every
proxy starts at the prior. The limit applies to the snapshot as a whole: a
fresh snapshot restores every record in it however old the feedback behind
it, and nothing ages running scores or feedback out while the service runs.

The optional cold-start mode is non-destructive:

- normal: restore and update the normal JSON state;
- `--no-restore`: skip JSON restore and write JSON only to a `.no-restore`
  sibling path.

`--no-restore` does not delete, rename, or overwrite normal state.

## 7. Configuration ownership

All thresholds above live in `[source_pool]` and are documented in
`config/config.example.ini`. Missing deployment keys are reported by
`check_config_drift()`. `config.ini` remains optional and git-ignored.

## 8. Operational observation

Local deterministic replay proves ordering and learning direction, not the
production ceiling. A rollout should run `--no-restore` or a shadow replay,
bucket requests by score before outcome, and compare the rolling success rate
with the observed stable wall. The target is 90-95% of that wall within roughly
one hour. Publish only aggregate bucket counts/rates; keep proxy addresses,
hostnames, internal domains, paths, and raw request logs private.
