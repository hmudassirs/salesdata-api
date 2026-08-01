# Phase 14 — measured optimisation

Per `docs/PerformancePlan.md`: "profile the current benchmark, select
one evidenced bottleneck, change it, run the same benchmark, and record
a before/after report. No blind optimisation." This is that report.

## 1. Profile the current benchmark

`benchmarks/benchmark_api.py --iterations 2000 --concurrency 20` (the
benchmark that already existed from Phase 12, unchanged) showed a much
larger profiling-enabled regression than `benchmark_timer.py`'s
microbenchmark alone would predict:

```text
baseline (baseline) vs candidate (candidate):
  throughput : 1851.5 -> 1062.9 ops/sec (-42.6%)
  p50 latency : 9.801ms -> 13.256ms (+35.2%)
  p90 latency : 12.204ms -> 35.108ms (+187.7%)
  p95 latency : 14.777ms -> 42.979ms (+190.9%)
  p99 latency : 35.097ms -> 91.094ms (+159.5%)

candidate trace-stage breakdown:
  request                  count=1000   mean=14.841ms p95=47.838ms
  dispatch                 count=1000   mean=14.802ms p95=47.824ms
  service_db_fetch_one     count=500    mean=0.040ms p95=0.092ms
```

The trace-stage breakdown itself pointed away from the SQL/pool
adapters that Phases 5–8 instrument: `service_db_fetch_one`'s own mean
(0.040ms) is negligible next to the ~14.8ms the request spends overall.
The regression had to be in the profiling machinery itself, not in an
instrumented subsystem — so the next step was profiling the benchmark
run with `cProfile`, not guessing:

```text
$ python3 /tmp/profile_candidate.py   # cProfile around _drive_load(), sorted by tottime
   ncalls  tottime  percall  cumtime  percall filename:lineno(function)
      500    0.043    0.000    0.043    0.000 core/performance/registry.py:91(_evict_oldest_index)
```

`_evict_oldest_index` alone accounted for 0.043s of the profiled run's
~2.4s total — and, more importantly, it was called exactly 500 times
for 1500 requests through a registry with the default
`max_request_history=1000`: once history fills, **every** subsequent
`record_completed_request` call triggers it (1500 - 1000 = 500 calls).

## 2. The evidenced bottleneck

```python
# core/performance/registry.py, before:
if (
    self.max_request_history
    and len(self._history_by_id) > self.max_request_history
):
    self._evict_oldest_index()

def _evict_oldest_index(self) -> None:
    retained_ids = {profile.request_id for profile in self._history}
    stale_ids = set(self._history_by_id) - retained_ids
    for stale_id in stale_ids:
        del self._history_by_id[stale_id]
```

`self._history` is a `deque(maxlen=max_request_history)`: it always
evicts exactly one item (the oldest) the moment a new item is appended
past capacity — the deque itself does this in O(1). But
`_history_by_id` is a separate plain `dict`, and the code above found
*which* id had fallen out of the deque by **rebuilding the entire
retained-id set from the deque** (an O(max_request_history) copy),
then taking a set difference against every key currently in the dict
(another O(max_request_history) pass) — to accomplish something that
only ever needs to evict one specific, already-knowable id. Once
history is at capacity, this O(N) rebuild runs on *every single
request* for the rest of the process's life, not just once — exactly
the pattern the profile caught.

## 3. The change

```python
# core/performance/registry.py, after:
evicted_id: str | None = None
if (
    self.max_request_history
    and len(self._history) >= self.max_request_history
):
    evicted_id = self._history[0].request_id
self._history.append(profile)
self._history_by_id[profile.request_id] = profile
if evicted_id is not None and evicted_id != profile.request_id:
    del self._history_by_id[evicted_id]
```

The entry about to fall out of the deque is `self._history[0]` — read
*before* the append that will evict it — so no rebuild is needed at
all: one index read, one dict delete, both O(1). `_evict_oldest_index`
is removed entirely. Behaviour is unchanged (verified by the existing
`test_registry.py` suite plus two new regression tests —
`test_history_by_id_stays_exactly_in_sync_past_capacity`, which drives
250 requests through a `max_request_history=10` registry and asserts
`_history_by_id` never drifts from `_history`, and
`test_recording_the_same_request_id_twice_does_not_evict_itself`, an
edge case the naive "delete the evicted id" version would get wrong if
a re-recorded request happened to share an id with the entry it's
about to push out).

## 4. Same benchmark, after

Two separate runs of the identical
`benchmarks.benchmark_api --iterations 2000 --concurrency 20` command:

```text
Run 1:
  throughput : 2064.4 -> 1543.0 ops/sec (-25.3%)     [was -42.6%]
  p50 latency : 8.670ms  -> 11.921ms (+37.5%)         [was +35.2%]
  p90 latency : 12.972ms -> 15.723ms (+21.2%)         [was +187.7%]
  p95 latency : 14.493ms -> 19.295ms (+33.1%)         [was +190.9%]
  p99 latency : 35.464ms -> 43.957ms (+23.9%)         [was +159.5%]

  candidate trace-stage breakdown:
    request   count=1000   mean=7.677ms p95=11.074ms   [was mean=14.841ms p95=47.838ms]

Run 2:
  throughput : 1959.8 -> 1670.9 ops/sec (-14.7%)
  p90 latency : 12.144ms -> 14.889ms (+22.6%)
  p95 latency : 14.212ms -> 19.520ms (+37.3%)
  p99 latency : 36.846ms -> 45.066ms (+22.3%)

  candidate trace-stage breakdown:
    request   count=1000   mean=6.940ms p95=9.062ms
```

The candidate run's own mean request time roughly halved (14.8ms ->
6.9-7.7ms) and its p95 improved by 4-5x (47.8ms -> 9-11ms) — and,
critically, the *gap* to baseline shrank in step: p90/p95/p99 went from
a 160-190% regression to a 20-37% one. Re-running the same `cProfile`
capture confirms `_evict_oldest_index` no longer appears anywhere in
the profile (it was deleted, and nothing replaced it as a comparable
cost).

The remaining baseline-vs-candidate gap is the real, expected cost of
profiling itself — trace-node allocation, `MetricPoint` construction,
histogram quantile updates (`histogram.py:_update_markers` is now
visible near the top of the `tottime` profile, at real per-call cost
rather than being dwarfed by an O(N) bug) — not an artifact of a
data-structure bug. `benchmarks/benchmark_timer.py`'s
`null-profiler`-vs-`real-profiler` comparison isolates that residual
cost directly if it's ever worth investigating further.
