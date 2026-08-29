# Proxy Quality: Scoring and Selection Boundaries

This spec records the design boundaries of the proxy quality system: which
signal owns which decision, and how aggressively a single observation is allowed
to move a proxy's rank. These are deliberate constraints, not accidents of the
current implementation — read this before "fixing" anything below.

Implementation: `src/core/proxy_manager.py`
(`_calculate_elo_score`, `_sync_and_select_top_proxies`, `get_proxy`).

---

## 1. Validation and feedback are decoupled on purpose

There are two independent signals about a proxy, and they own strictly separate
decisions:

| Signal | Source | Owns | Recorded in |
| --- | --- | --- | --- |
| **Validation** | The service's own periodic probe of `validation_target(s)` | **Liveness only** — the `is_active` gate: may this proxy be handed out at all? | `proxies.is_active` in PostgreSQL |
| **Feedback** | `POST /feedback` from real client traffic | **Ranking** — the 0-100 score that decides pool membership and selection weight | `source_stats` in memory, backed up to JSON |

### What this permits

Filtering the dispatch pool by `is_active` — a proxy the last validation cycle
declared dead must not appear in `top_tier` / `bottom_tier`, regardless of its
score. That is the liveness gate doing its job.

### What this forbids

Feeding validation measurements — the probe's latency, its `anonymity_level`,
its pass/fail — into the ELO score. That is **signal fusion**, and it is
rejected.

### Why

The two signals measure different things against different targets. Validation
measures one proxy against one fixed URL that the operator chose; feedback
measures the same proxy against whatever the client is actually doing. A proxy
that is fast to `httpbin.org` and useless against the real target is common, and
so is the reverse. Mixing them produces a score that answers neither question:
a proxy could be propped up by good probe latency despite failing every real
request, or buried by a rate-limited probe target despite serving traffic fine.

Keeping them separate also keeps the failure modes legible. When the pool goes
bad, exactly one of two things is true — either the liveness gate is admitting
dead proxies (a validation problem) or the ranking is wrong (a feedback
problem). Fused, every incident becomes an argument about weights.

**Corollary**: ranking defects must be fixed inside the feedback/exploration
system. When thousands of proxies tie at the neutral 50.0 and the top pool ends
up decided by dict insertion order, the fix is an explicit exploration budget
(`exploration_ratio`), which buys real feedback for untried proxies — not a
tiebreak on validation latency, which would smuggle the probe signal into the
ranking through the back door.

---

## 2. Scoring is optimistic, but one observation is not a coronation

A new proxy that succeeds a few times is *supposed* to score above the untried
baseline. That head start is an **exploration budget**, not a bug: without it a
newly discovered proxy has no way to accumulate the traffic it needs to prove
itself, and the pool ossifies around whatever was in it at startup.

What is a bug is the *magnitude*. Before this was constrained, the raw success
ratio let one lucky observation reach a perfect 1.0, so a single success scored
95.0 — above a proxy with 48 successes out of 50 — and the top of the pool
filled up with one-hit wonders.

### The constraint

The observed success rate is shrunk toward the 0.5 baseline by a Beta prior
(`elo_prior_successes` / `elo_prior_failures`, both 2.0 by default):

```
smoothed_rate = (successes_weight + a) / (total_weight + a + b)
```

Calibration targets, at the defaults, with fresh results and 200ms latency:

| Evidence | Score | Requirement |
| --- | --- | --- |
| no observations | 50.0 | the neutral baseline |
| 1 success | ~59 | **above** the baseline, in the 50-75 band |
| 48 of 50 successes | ~90 | **well above** any small sample |
| 1 failure | ~29 | punished, but recoverable |
| 50 of 50 failures | ~2 | effectively out |
| 35% success rate | ~32 | **below** the untried baseline |

The rule that ties these together: a small sample must land strictly between the
untried baseline and a proven proxy. Never below (that removes the incentive to
try anything new), never above (that hands the pool to noise).

### Recovery from a single failure

The mirror image of "one success must not crown" is "one failure must not
exile". A single fresh failure scores ~29, climbs slowly as the result decays,
and returns to the 50 baseline once the result passes
`elo_max_result_age_hours`.

That threshold is therefore the real knob for how long one bad result costs a
proxy its traffic — while it sits below the baseline it is outside the top pool,
so it collects no feedback and has nothing to recover *with*, apart from the
`exploration_ratio` budget. The shipped default is 48 hours rather than the
7 days it started at, because a week below baseline for a single failure is
punitive enough to recreate the exile this system exists to prevent.

Two things have to hold for that sentence to be true, and both are easy to break
by accident:

- **The cumulative counters are not a fallback for an expired window.**
  `success_count` / `failure_count` never expire. Reading them when the window
  has emptied re-applies the very result `elo_max_result_age_hours` just forgave,
  and a single failure then decays asymptotically toward 50 without ever
  arriving — 40.3 at 49 hours with the shipped half-life. So the scorer
  distinguishes *never observed* (fall back to the counters; a stat restored
  from an old backup has nothing else) from *observed, then aged out* (return
  the 50 baseline outright).
- **Expiry and exploration eligibility use one definition.** The exploration
  budget is the only way back for a proxy scoring below the baseline, so if it
  asks "does this proxy have a result?" while the scorer asks "does it have a
  result that still counts?", a proxy is exiled by evidence the scorer has
  already discarded. `_unexpired_results()` is that single definition, and both
  call it.

Note the recovery curve has a discontinuity at that threshold (the result is
dropped outright rather than fading out). Smoothing it would mean blending the
whole score toward 50 by evidence weight, which measurably pushes a 48-of-50
proxy below the 90-point target above — i.e. it requires re-tuning the
component weights, not just adding a blend. Deliberately not done.

The measured curve for one failure at the shipped defaults: 29.0 fresh, 31.7 at
24h, 33.2 at 47h, 50.0 from 48h on.

### What this forbids

Making new proxies start low, or start at zero, or serve a probationary sentence
before they can be selected. If one-hit wonders crowd the pool, raise the prior
(`elo_prior_successes` / `elo_prior_failures`) so small samples shrink harder
toward 0.5 — do not move the starting point.

---

## 3. Ranking staleness of one validation cycle is accepted

Scores are recomputed for the whole pool during `_sync_and_select_top_proxies`,
which runs once per validation cycle (`validation_interval_seconds`, 120s by
default), not on every `/feedback` call.

Full-pool recomputation *must* happen somewhere: scores that are only refreshed
when feedback arrives freeze the moment a proxy stops receiving traffic, which
means time decay never fires and a proxy knocked out of the top pool by one bad
result can never be reconsidered. Recomputing per feedback event would be the
alternative, and it is rejected as an unnecessary cost — the measured cost of a
full-pool pass is ~32ms for 4000 proxies across 2 sources, the same order as the
sync it runs inside, and one cycle of ranking lag is not worth avoiding it.

---

## 4. Quality is expressed as score, never as a deletion rule

There is no "N consecutive failures and it's gone" rule. `consecutive_failures`
is a **diagnostic field only** and must not gate selection.

A bad proxy loses traffic because its score falls, and it regains traffic if its
score recovers. One mechanism, one place to reason about, one place to tune. A
hard-deletion threshold would add a second, non-recoverable path with its own
edge cases — and would interact badly with the decoupling in §1, because a
transient run of client-side failures is not evidence that a proxy is dead.

Reputation must also survive pool maintenance. **Eviction from the stats pool is
reputation loss**, because the record does not survive it: `_sync_and_select_top_proxies`
re-seeds any active proxy missing from the pool with `_get_new_proxy_stat()`, so
an evicted-but-still-active proxy returns one cycle later as a pristine
`score=50 / failure_count=0` candidate.

That makes the eviction *order* the wrong thing to tune. Any order that can
evict a live proxy launders a bad record on a two-sync delay — which is the same
defect as evicting by score outright, just slower to observe. So:

**Live proxies do not participate in the cap.** It applies to dead history
alone, oldest feedback first. Dead history is the part that is safe to drop: a
dead proxy that comes back has to pass validation again anyway, and until it
does it cannot be handed out.

The consequence is that `max_pool_size × stats_pool_max_multiplier` bounds
retained *dead* history, not total memory — the live half tracks however many
proxies are genuinely active. When the live set alone reaches the cap, all dead
entries are dropped and a warning names the number, so the operator can raise
`stats_pool_max_multiplier` or lower `max_pool_size` rather than silently
trading away reputation.

---

## 5. New tunables go to config

Every threshold introduced here is a config key in `[source_pool]` or
`[validator]` with a fallback default, and appears in `config/config.example.ini`.
No new magic numbers in code. `config.ini` is git-ignored, so the service must
run correctly when a key is absent — and warn about it: `check_config_drift()`
diffs the live config against the example at startup and logs every key that is
silently falling back.
