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
system. When thousands of untried proxies tie at the baseline and the top pool
ends up decided by dict insertion order, the fix is an explicit exploration budget
(`exploration_ratio`), which buys real feedback for untried proxies — not a
tiebreak on validation latency, which would smuggle the probe signal into the
ranking through the back door.

---

## 2. The untried baseline is the pool's median, not a constant

A proxy with no usable evidence scores the **median score of the live proxies
that do have evidence**, recomputed once per pool sync. Every "nothing to score
on" path returns it: never observed, observed then aged out, and historical
counters with no usable timestamp. One baseline, one place to reason about it.

### Why not a constant

A fixed 50.0 is only meaningful while the pool's real scores straddle 50. They
do not have to. When the population's honest success rate is low - and for free
proxies it is - every score that reflects evidence sits below 50, and the
ranking inverts wholesale: a proxy that has *never been measured* outranks every
proxy that has, because 50 beats all of them. The pool then fills with proxies
whose only qualification is that nothing is known about them, which is the
opposite of what the score is for.

This was observed, not hypothesised. The `insolvencydirect` pool reached a state
where its 4000 stats entries topped out at 42.63, no proxy scored 50 or more,
and all 100 top-tier slots were held by unmeasured proxies while 44 proxies with
a proven record (>=20 observations, >=40% window success rate) sat outside the
pool entirely. End-to-end success followed the ranking down from 48% to 11.6%.

Pinning the baseline to the middle of the measured distribution makes "unknown"
mean *mid-pack* whatever the absolute numbers are: half the proven proxies rank
above an unknown one, half below, and the ordering survives any recalibration of
the components.

*(Empirical note from Issue #21)*: In an environment with low baseline success (~10%),
untried proxies (4.2% forward value) actually sit toward the lower-middle of the
distribution rather than symmetric mid-pack. But the median baseline (~10-12 points)
places untried proxies in a sound position: strictly below proven proxies (31.6-39.9%
forward value, scoring ~35-65) and strictly above consecutive failures (0.1-1.9%
forward value, scoring 2-5).

### Which proxies vote

Only proxies that are **live** and **measured**.

- Unmeasured ones are excluded because their score *is* the baseline. Including
  them makes the median a fixed point of itself - when most of the pool is
  unmeasured, "the median of everyone" is just whatever the baseline already
  was, and any value is self-consistent.
- Dead ones are excluded because they cannot be handed out, so they say nothing
  about what a fresh candidate is competing against.

This is why `_sync_and_select_top_proxies` rescores in two passes: measured
proxies first (their scores do not depend on the baseline), then the baseline is
read off them, then the unmeasured ones are scored against it.

With nothing measured - an empty pool, a fresh restart, a source whose every
live proxy is still untried - there is no distribution to take a median of, and
the baseline falls back to the `DEFAULT_NEUTRAL_SCORE` constant of 50.0.

### What this changes about §3 below

The head start a lucky new proxy gets is now *relative*. A single success scores
~55; whether that is above the baseline depends on the pool. In a pool whose
median is 60, one success does not vault a proxy to the front - and it should
not. The guarantee that untried proxies keep collecting evidence is
`exploration_ratio`, exactly as §1's corollary says; it was never the starting
score's job.

---

## 3. Scoring is optimistic, but one observation is not a coronation

A new proxy that succeeds a few times is *supposed* to gain on the untried
baseline. That head start is an **exploration budget**, not a bug: without it a
newly discovered proxy has no way to accumulate the traffic it needs to prove
itself, and the pool ossifies around whatever was in it at startup. Since §2 the
head start is relative — it lifts a proxy through the measured distribution, not
past a fixed number.

What is a bug is the *magnitude*. Before this was constrained, the raw success
ratio let one lucky observation reach a perfect 1.0, so a single success scored
95.0 — above a proxy with 48 successes out of 50 — and the top of the pool
filled up with one-hit wonders.

### The constraint

The observed success rate is smoothed by a Beta prior
(`elo_prior_successes` = 0.25 / `elo_prior_failures` = 0.75 by default):

```
smoothed_rate = (successes_weight + a) / (total_weight + a + b)
```

*(Revision from Issue #21)*: The earlier spec assumed a symmetric prior `a=b=2.0`
centering at 0.5. In populations where the real base success rate is ~10%, shrinking
toward 0.5 inappropriately inflated failing proxies: a proxy with multiple consecutive
failures still scored 17-25 points, ranking above untried proxies (~11.6). Weakening
the prior to `a=0.25, b=0.75` (strength 1.0, center 0.25) allows evidence to govern
quickly while maintaining smooth regularization. Furthermore, new proxies (<10 results)
default to `elo_new_proxy_consistency_bonus = 0.0` rather than receiving an unconditional
5-point bonus.

Calibration targets, at the defaults, with fresh results and target response latencies:

| Evidence | Score | Requirement |
| --- | --- | --- |
| no observations | the pool median (~10-12) | see §2 |
| 1 success | ~55 | **above** untried and failing proxies |
| 48 of 50 successes | ~90+ | **well above** any small sample |
| 40% success rate (20 of 50) | ~38-45 | **substantially above** untried baseline |
| 1 failure | ~7.5 | recoverable, but below untried baseline |
| 2 consecutive failures | ~5.0 | strictly below untried baseline |
| 50 of 50 failures | ~0.04 | effectively out |

### The latency component has to be calibrated on the real population

`latency_full_score_ms` / `latency_zero_score_ms` are not preferences, they are
a statement about where this population's latencies fall. Anything at or past
the zero point scores 0, so thresholds set below the population put *every*
proxy at 0 and silently delete the whole 30-point component - the ranking then
runs on success rate and consistency alone and nobody notices, because no error
is raised and the scores still look like scores.

*(Revision from Issue #21)*: Feedback latency measures the **target website's response time
via the proxy**, not bare proxy connect latency. For slow target sites (e.g. 15s-30s),
the earlier 5000/30000 thresholds compressed the best proxies' 30-point latency component
by ~80%. The recalibrated defaults `latency_full_score_ms = 15000` and `latency_zero_score_ms = 60000`
provide proper discrimination across real target response times.

### Recovery from a single failure and ranking order

A single fresh failure scores ~7.5, climbs slowly as the result decays,
and returns to the untried baseline once the result passes `elo_max_result_age_hours` (48 hours).
Crucially, proxies that suffer 2+ consecutive failures score 2-5 points, ensuring that
proven failures are not promoted over untried proxies.

### Recency Circuit Breaker

To prevent sudden outages or target bans from slowly bleeding client requests over a 50-item
window, scoring incorporates a **Recency Circuit Breaker**:
When a proxy has at least 10 observations (`len(recent) >= 10`) and its most recent 10 results
are all failures (`sum(recent_10_successes) == 0`), a steep penalty multiplier (`0.15`) is
applied to its raw score. This plunges even a previously high-scoring (~70 point) proxy to
<8 points, dropping it below the untried baseline (<10) and evicting it from the top pool on
the next sync cycle (within 120s).

### Softmax Selection Strategy

To avoid the collapse of selection discrimination under linear weighting (`max(floor, score)`),
the default selection strategy is `softmax` with `softmax_temperature = 14.0`. This provides
an exponential advantage (~30:1 or more) for high-performing nodes over mediocre ones while
avoiding the catastrophic hard-cutoff hazards of small fixed pool sizes.

---

## 4. Ranking staleness of one validation cycle is accepted

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

## 5. Quality is expressed as score, never as a deletion rule

There is no "N consecutive failures and it's gone" rule. `consecutive_failures`
is a **diagnostic field only** and must not gate selection.

A bad proxy loses traffic because its score falls, and it regains traffic if its
score recovers. One mechanism, one place to reason about, one place to tune. A
hard-deletion threshold would add a second, non-recoverable path with its own
edge cases — and would interact badly with the decoupling in §1, because a
transient run of client-side failures is not evidence that a proxy is dead.

Reputation must also survive pool maintenance. **Eviction from the stats pool
used to be reputation loss**, because the record did not survive it:
`_sync_and_select_top_proxies` re-seeds any active proxy missing from the pool
with `_get_new_proxy_stat()`, so an evicted proxy returned one cycle later as a
pristine `failure_count=0` candidate.

Two things address that, and both are needed.

**The counters are durable.** `proxies.feedback_success_count` /
`feedback_failure_count` / `feedback_last_ts` hold a copy of the in-memory
counters, written back for proxies whose feedback moved since the last flush.
When the sync re-seeds a proxy it seeds those counters too, so the proxy comes
back with the record it earned rather than a clean sheet, and time decay - not
eviction - is what eventually forgives it. Absolute totals are written, not
increments, so a write lost to a transient DB error is corrected by the next
one instead of leaving the stored total permanently short.

This is not a substitute for the eviction rule below. The stored record has no
sliding window, so a restored proxy scores off the historical counters, which
decay toward the baseline with age; a proxy evicted while still live would still
lose its recent-window evidence. The durable counters close the *revival* path,
which the cap alone could not.

That makes the eviction *order* the wrong thing to tune. Any order that can
evict a live proxy launders a bad record on a two-sync delay — which is the same
defect as evicting by score outright, just slower to observe. So:

**Live proxies do not participate in the cap.** It applies to dead history
alone, oldest feedback first. Dead history is the safest part to drop: a dead
proxy that comes back has to pass validation again anyway, until it does it
cannot be handed out, and its counters survive in the proxies table.

The consequence is that `max_pool_size × stats_pool_max_multiplier` bounds
retained *dead* history, not total memory — the live half tracks however many
proxies are genuinely active. When the live set alone reaches the cap, all dead
entries are dropped and a warning names the number, so the operator can raise
`stats_pool_max_multiplier` or lower `max_pool_size` rather than silently
trading away reputation.

---

## 6. New tunables go to config

Every threshold introduced here is a config key in `[source_pool]` or
`[validator]` with a fallback default, and appears in `config/config.example.ini`.
No new magic numbers in code. `config.ini` is git-ignored, so the service must
run correctly when a key is absent — and warn about it: `check_config_drift()`
diffs the live config against the example at startup and logs every key that is
silently falling back.
