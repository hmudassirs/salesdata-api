"""CPU-count detection and core-count-derived sizing for pools/executors
(roadmap gap noted after the initial refactor: every pool/executor size
was a hardcoded constant -- 10 DB connections, 12 db_executor threads,
etc -- picked for whatever machine happened to run the load test, not
derived from the hardware actually available. That means the same
config either starves a bigger box or oversubscribes a smaller one.

This module answers one question -- "how much real parallelism does
this process actually have?" -- as carefully as reasonably possible,
then turns that into sizing recommendations for the different kinds of
pools in this codebase, since "more threads" isn't the right answer for
every one of them (see `recommended_sizing()`'s docstring).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional


def detect_cpu_count() -> int:
    """Best-effort count of CPUs actually available to this process.

    Checked in order, falling back if a step is unavailable/unreliable:

    1. `os.sched_getaffinity(0)` (Linux only) -- the number of CPUs this
       *process* is allowed to run on. Respects `taskset`/Kubernetes
       CPU-pinning (`cpuset`), which `os.cpu_count()` does not: on a
       32-core host with this process pinned to 4 cores,
       `os.cpu_count()` still reports 32.
    2. cgroup CPU quota (v2 `cpu.max`, v1 `cpu.cfs_quota_us` /
       `cpu.cfs_period_us`) -- respects Docker/Kubernetes `--cpus`
       *limits*, which affinity does not: a container capped at "2.0
       CPUs" on an 8-core host with no cpuset restriction still shows
       affinity=8, but can only actually get ~2 cores of work done.
       Takes the *lower* of the affinity count and the quota, since
       either one can be the binding constraint.
    3. `os.cpu_count()` -- total logical CPUs on the host. Last resort:
       correct on bare metal / a VM with no further restrictions, an
       overestimate in a constrained container.
    4. `1` -- if every signal above is unavailable, assume no usable
       parallelism rather than guessing.
    """
    count: Optional[int] = None

    if hasattr(os, "sched_getaffinity"):
        try:
            count = len(os.sched_getaffinity(0))
        except Exception:
            count = None

    quota = _cgroup_cpu_quota()
    if quota is not None:
        count = min(count, quota) if count is not None else quota

    if count is None:
        count = os.cpu_count()

    return max(1, count or 1)


def _cgroup_cpu_quota() -> Optional[int]:
    """Read a cgroup CPU limit, rounded down to whole CPUs. None if no
    limit is set (or not running under a cgroup with one)."""
    # cgroup v2: single file, "$MAX $PERIOD" in microseconds, or "max"
    # for "no limit".
    try:
        with open("/sys/fs/cgroup/cpu.max") as f:
            max_str, period_str = f.read().split()
        if max_str != "max":
            quota = int(max_str) / int(period_str)
            return max(1, int(quota))
    except (FileNotFoundError, ValueError, OSError):
        pass

    # cgroup v1: two files; quota of -1 means "no limit".
    try:
        with open("/sys/fs/cgroup/cpu/cpu.cfs_quota_us") as f:
            quota_us = int(f.read().strip())
        if quota_us > 0:
            with open("/sys/fs/cgroup/cpu/cpu.cfs_period_us") as f:
                period_us = int(f.read().strip())
            return max(1, int(quota_us / period_us))
    except (FileNotFoundError, ValueError, OSError):
        pass

    return None


@dataclass(frozen=True)
class ConcurrencySizing:
    """Core-count-derived defaults for every bounded pool in this
    codebase. All of these remain overridable via explicit config/env
    vars (see AppSettings/run_api.py) -- this is only the *fallback*
    used when nothing more specific was configured, computed instead of
    hardcoded so a bigger or smaller box gets a sensible default
    without editing code.
    """

    cpu_count: int

    # --- DuckDB warehouse pool ---
    # DuckDB is embedded and CPU-bound: a single query already uses
    # multiple internal threads (its default is the host's logical CPU
    # count). Unlike a network-bound DB pool, adding *more concurrent
    # connections* than there are cores doesn't add throughput past a
    # point -- it adds context-switch/cache-thrashing overhead between
    # DuckDB's own worker threads across connections. So this pool is
    # sized to the core count itself, not some multiple of it.
    db_pool_min: int
    db_pool_max: int

    # --- SQLite service DB pool (auth, cache, logging) ---
    # SQLite work here is small, mostly-indexed reads/writes -- more
    # I/O-wait-bound than the warehouse pool, so a modest multiple of
    # cores is reasonable (more concurrent waiters than cores, since
    # they spend part of their time blocked on disk/WAL, not compute).
    service_pool_min: int
    service_pool_max: int

    # --- Dedicated executors (core/concurrency/executors.py) ---
    # Each executor's threads mostly *wait* on a pool acquire + a
    # blocking driver call, so it's sized a little above its matching
    # pool's max, not equal to it -- enough headroom that a connection
    # freeing up doesn't have to wait for a thread to free up too.
    db_executor_workers: int
    service_executor_workers: int
    # Background work (cache persistence, access-stat writes) never
    # needs to track pool size the way request-serving executors do --
    # it's explicitly OK for this to be smaller, since it exists to
    # *not* compete with request-serving threads (see
    # core/concurrency/executors.py's module docstring).
    background_executor_workers: int

    # --- Query cost-class concurrency semaphores (roadmap Phase 14) ---
    # These gate "how many callers may be mid-execution", not DB
    # connections directly, so they can reasonably exceed the DB pool
    # size (excess callers queue on the pool itself, which is the real
    # ceiling) -- but scaling them off core count instead of a fixed
    # constant means a bigger box can actually let more through before
    # that queuing kicks in.
    fast_query_concurrency: int
    normal_query_concurrency: int
    expensive_query_concurrency: int


def recommended_sizing(cpu_count: Optional[int] = None) -> ConcurrencySizing:
    """Compute recommended pool/executor sizes from a CPU count.

    Args:
        cpu_count: override for testing; defaults to `detect_cpu_count()`.
    """
    n = cpu_count if cpu_count is not None else detect_cpu_count()

    return ConcurrencySizing(
        cpu_count=n,
        db_pool_min=max(2, n // 2),
        db_pool_max=max(4, n),
        service_pool_min=max(2, n // 2),
        service_pool_max=max(4, n * 2),
        db_executor_workers=max(4, n) + 2,
        service_executor_workers=max(4, n * 2) + 2,
        background_executor_workers=max(2, n),
        fast_query_concurrency=max(20, n * 25),
        normal_query_concurrency=max(8, n * 10),
        expensive_query_concurrency=max(2, n),
    )
