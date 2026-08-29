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

Reputation must also survive pool maintenance. When the stats pool exceeds its
cap it is truncated by **staleness** (`last_feedback_ts`), never by score: a
proxy evicted for scoring badly would re-enter on the next sync as a pristine
`score=50 / failure_count=0` candidate and displace peers that still carry their
record — laundering exactly the history the score exists to remember.

---

## 5. New tunables go to config

Every threshold introduced here is a config key in `[source_pool]` or
`[validator]` with a fallback default, and appears in `config/config.example.ini`.
No new magic numbers in code. `config.ini` is git-ignored, so the service must
run correctly when a key is absent — and warn about it: `check_config_drift()`
diffs the live config against the example at startup and logs every key that is
silently falling back.
