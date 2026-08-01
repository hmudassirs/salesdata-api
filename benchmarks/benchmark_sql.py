#!/usr/bin/env python3
"""Benchmark `core.storage.service_db.ServiceDatabase` execute/fetch throughput.

Compares, against a real temp-file SQLite database seeded with 200 rows:

- `raw-sql`         : call `ServiceDatabase.fetch_one` directly.
- `instrumented-sql`: the same database wrapped in
                       `core.performance.adapters.sqlite.InstrumentedServiceDatabase`,
                       with no profiler bound (the common, unsampled case).
- `instrumented-sql-profiled`: the same wrapped database, with a real
                       `RequestProfiler` bound for every call — what a
                       *sampled* request actually pays.

Usage:
    python -m benchmarks.benchmark_sql
    python -m benchmarks.benchmark_sql --iterations 2000
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from benchmarks._common import (
    build_arg_parser,
    render_report,
    run_benchmark,
    write_json_results,
)
from core.performance.adapters.sqlite import InstrumentedServiceDatabase
from core.performance.context import bind_profiler
from core.performance.request_profiler import RequestProfiler
from core.storage.service_db import ServiceDatabase

_SEED_ROWS = 200
_SELECT_SQL = "SELECT username FROM users WHERE user_id = ?"


def _build_seeded_database(db_path: Path) -> ServiceDatabase:
    db = ServiceDatabase(db_path=str(db_path), min_size=2, max_size=8)
    db.connect()
    db.create_tables()
    for i in range(_SEED_ROWS):
        db.execute(
            "INSERT INTO users "
            "(user_id, username, email, password_hash, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (f"u{i}", f"user{i}", f"user{i}@example.com", "hash", 1, 1),
        )
    return db


def main() -> None:
    parser = build_arg_parser(__doc__ or "")
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        raw_db = _build_seeded_database(tmp_path / "raw.db")
        instrumented_db = InstrumentedServiceDatabase(
            _build_seeded_database(tmp_path / "instrumented.db")
        )
        profiled_db = InstrumentedServiceDatabase(
            _build_seeded_database(tmp_path / "profiled.db")
        )

        def _run_profiled() -> None:
            with bind_profiler(RequestProfiler()):
                profiled_db.fetch_one(_SELECT_SQL, ("u1",))

        results = [
            run_benchmark(
                "raw-sql",
                args.iterations,
                lambda: raw_db.fetch_one(_SELECT_SQL, ("u1",)),
            ),
            run_benchmark(
                "instrumented-sql",
                args.iterations,
                lambda: instrumented_db.fetch_one(_SELECT_SQL, ("u1",)),
            ),
            run_benchmark(
                "instrumented-sql-profiled", args.iterations, _run_profiled
            ),
        ]

        raw_db.disconnect()
        instrumented_db.disconnect()
        profiled_db.disconnect()

    print(render_report(results))  # noqa: T201
    if args.json_out:
        write_json_results(args.json_out, results)


if __name__ == "__main__":
    main()
