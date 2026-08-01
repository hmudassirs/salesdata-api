#!/usr/bin/env python3
"""
Post-restructure verification for the PrepareData API.

Three phases:
  1. Functional smoke tests — one request per route, checking each piece
     touched by the restructure actually still works end to end (auth
     bootstrap, health, query, tables, the new API-key validation cache).
  2. The original concurrent load test (unchanged behavior), so you can
     confirm throughput/correctness didn't regress alongside the
     structural changes.
  3. Report assembly — every result from phases 1-2, plus (optionally)
     the server's own `/debug/performance` snapshot taken right after
     the load test, is written to one timestamped JSON file for
     API-optimization analysis. See `build_report` / `--output`.

Requires: httpx   ->   pip install httpx

Usage:
    python load_test.py                       # both phases, --auto-auth
    python load_test.py --smoke-only           # just phase 1
    python load_test.py --load-only --api-key YOUR_KEY
    python load_test.py --concurrency 200 --sql "SELECT 1"
    python load_test.py --output my_run.json
    python load_test.py --dashboard-api-key ADMIN_KEY   # attach server-side
                                                         # /debug/performance
                                                         # snapshot to the report
"""

import argparse
import asyncio
import json
import secrets
import statistics
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx


@dataclass
class RequestResult:
    status_code: int
    elapsed_ms: float
    error: Optional[str] = None
    body: Optional[dict] = None


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str = ""


# ---------------------------------------------------------------------
# Phase 1: functional smoke tests
# ---------------------------------------------------------------------


async def obtain_api_key(
    client: httpx.AsyncClient, base_url: str, username: str, password: str, email: str
) -> tuple[str, str]:
    """Register (fine if it already exists), log in, mint an API key.

    Returns (api_key, user_id).
    """
    await client.post(
        f"{base_url}/api/auth/users/register",
        json={"username": username, "password": password, "email": email},
    )

    login_resp = await client.post(
        f"{base_url}/api/auth/users/login",
        json={"username": username, "password": password},
    )
    login_resp.raise_for_status()
    login_data = login_resp.json()
    if not login_data.get("success"):
        raise RuntimeError(f"Login failed: {login_data.get('message')}")

    token = login_data["token"]
    user_id = login_data["user"]["user_id"]

    # Exercises the JWT-bootstrap auth path (core/auth/middleware.py) —
    # this call has no x-api-key yet, only the login JWT as bearer auth.
    key_resp = await client.post(
        f"{base_url}/api/auth/keys",
        json={"owner_id": user_id, "scopes": "read,write"},
        headers={"Authorization": f"Bearer {token}"},
    )
    key_resp.raise_for_status()
    return key_resp.json()["api_key"], user_id


async def run_smoke_tests(
    client: httpx.AsyncClient, base_url: str, api_key: str, user_id: str
) -> list[CheckResult]:
    results: list[CheckResult] = []
    headers = {"x-api-key": api_key}

    # --- health: core/app/health.py + core/db pool metrics ---
    try:
        r = await client.get(f"{base_url}/api/health", headers=headers)
        ok = r.status_code == 200 and r.json().get("status") in ("healthy", "degraded")
        results.append(
            CheckResult(
                "GET /api/health", ok, f"status={r.status_code} body={r.text[:150]}"
            )
        )
    except Exception as e:
        results.append(CheckResult("GET /api/health", False, str(e)))

    # --- query: core/app/api/routes/query.py + DuckDB pool ---
    try:
        r = await client.post(
            f"{base_url}/api/query",
            json={"sql": "SELECT 1 AS ok", "params": None},
            headers=headers,
        )
        ok = r.status_code == 200 and r.json().get("success") is True
        results.append(
            CheckResult(
                "POST /api/query (SELECT 1)",
                ok,
                f"status={r.status_code} body={r.text[:150]}",
            )
        )
    except Exception as e:
        results.append(CheckResult("POST /api/query (SELECT 1)", False, str(e)))

    # --- query result cache: same SELECT again, expect cached=True ---
    try:
        r = await client.post(
            f"{base_url}/api/query",
            json={"sql": "SELECT 1 AS ok", "params": None},
            headers=headers,
        )
        cached = r.status_code == 200 and r.json().get("cached") is True
        results.append(
            CheckResult(
                "POST /api/query (repeat -> cached)",
                cached,
                f"cached={r.json().get('cached') if r.status_code == 200 else 'n/a'}",
            )
        )
    except Exception as e:
        results.append(CheckResult("POST /api/query (repeat -> cached)", False, str(e)))

    # --- tables: core/app/api/routes/query.py ---
    try:
        r = await client.get(f"{base_url}/api/tables", headers=headers)
        ok = r.status_code == 200 and "tables" in r.json()
        results.append(
            CheckResult(
                "GET /api/tables", ok, f"status={r.status_code} body={r.text[:150]}"
            )
        )
    except Exception as e:
        results.append(CheckResult("GET /api/tables", False, str(e)))

    # --- api key listing: core/app/api/routes/auth.py + auth ownership check ---
    try:
        r = await client.get(f"{base_url}/api/auth/keys/{user_id}", headers=headers)
        ok = r.status_code == 200 and r.json().get("count", 0) >= 1
        results.append(
            CheckResult(
                "GET /api/auth/keys/{owner_id}",
                ok,
                f"status={r.status_code} body={r.text[:150]}",
            )
        )
    except Exception as e:
        results.append(CheckResult("GET /api/auth/keys/{owner_id}", False, str(e)))

    # --- repeated auth with the same key in quick succession: exercises
    # the new APIKeyService validation cache (core/auth/api_key_service.py)
    # without a way to directly observe cache hits from outside, this at
    # least confirms the cache doesn't silently break auth correctness.
    try:
        oks = []
        for _ in range(5):
            r = await client.post(
                f"{base_url}/api/query",
                json={"sql": "SELECT 1 AS ok", "params": None},
                headers=headers,
            )
            oks.append(r.status_code == 200)
        results.append(
            CheckResult(
                "5x rapid requests, same key (validation cache)",
                all(oks),
                f"statuses_ok={oks}",
            )
        )
    except Exception as e:
        results.append(
            CheckResult("5x rapid requests, same key (validation cache)", False, str(e))
        )

    # --- bad key rejected ---
    try:
        r = await client.post(
            f"{base_url}/api/query",
            json={"sql": "SELECT 1", "params": None},
            headers={"x-api-key": "not_a_real_key"},
        )
        ok = r.status_code == 401
        results.append(
            CheckResult(
                "POST /api/query (bad key -> 401)", ok, f"status={r.status_code}"
            )
        )
    except Exception as e:
        results.append(CheckResult("POST /api/query (bad key -> 401)", False, str(e)))

    # --- no key at all rejected ---
    try:
        r = await client.post(
            f"{base_url}/api/query", json={"sql": "SELECT 1", "params": None}
        )
        ok = r.status_code == 401
        results.append(
            CheckResult(
                "POST /api/query (no key -> 401)", ok, f"status={r.status_code}"
            )
        )
    except Exception as e:
        results.append(CheckResult("POST /api/query (no key -> 401)", False, str(e)))

    return results


def print_smoke_report(results: list[CheckResult]):
    print("\n" + "=" * 60)
    print("Phase 1: functional smoke tests")
    print("=" * 60)
    for r in results:
        mark = "PASS" if r.passed else "FAIL"
        print(f"  [{mark}] {r.name}")
        if not r.passed:
            print(f"         {r.detail}")
    passed = sum(1 for r in results if r.passed)
    print("-" * 60)
    print(f"{passed}/{len(results)} checks passed")
    print("=" * 60 + "\n")


# ---------------------------------------------------------------------
# Phase 2: concurrent load test (unchanged behavior from before)
# ---------------------------------------------------------------------


async def run_one_query(
    client: httpx.AsyncClient, base_url: str, api_key: str, sql: str, params: list
) -> RequestResult:
    start = time.perf_counter()
    try:
        resp = await client.post(
            f"{base_url}/api/query",
            json={"sql": sql, "params": params},
            headers={"x-api-key": api_key},
        )
        elapsed_ms = (time.perf_counter() - start) * 1000
        try:
            body = resp.json()
        except Exception:
            body = None
        return RequestResult(
            status_code=resp.status_code, elapsed_ms=elapsed_ms, body=body
        )
    except Exception as e:
        elapsed_ms = (time.perf_counter() - start) * 1000
        return RequestResult(
            status_code=0, elapsed_ms=elapsed_ms, error=str(e) or repr(e)
        )


def percentile(data: list, pct: float) -> float:
    if not data:
        return 0.0
    data = sorted(data)
    k = (len(data) - 1) * (pct / 100)
    f = int(k)
    c = min(f + 1, len(data) - 1)
    if f == c:
        return data[f]
    return data[f] + (data[c] - data[f]) * (k - f)


def build_load_report(
    results: list[RequestResult], total_wall_time: float, concurrency: int, sql: str
) -> dict:
    """Turn raw per-request results into the JSON-serializable summary
    that both `print_load_report` and `build_report` (the on-disk JSON)
    read from — one computation, two consumers, so the printed numbers
    and the saved numbers can never drift apart.
    """
    latencies = [r.elapsed_ms for r in results]
    ok = [r for r in results if r.status_code == 200 and not r.error]
    failed = [r for r in results if r not in ok]

    status_counts: dict[str, int] = {}
    for r in results:
        key = str(r.status_code) if r.status_code else "connection_error"
        status_counts[key] = status_counts.get(key, 0) + 1

    return {
        "sql": sql,
        "concurrency": concurrency,
        "total_requests": len(results),
        "wall_time_seconds": total_wall_time,
        "throughput_req_per_sec": len(results) / total_wall_time if total_wall_time else 0.0,
        "succeeded": len(ok),
        "failed": len(failed),
        "status_code_counts": status_counts,
        "latency_ms": {
            "min": min(latencies) if latencies else 0.0,
            "avg": statistics.mean(latencies) if latencies else 0.0,
            "max": max(latencies) if latencies else 0.0,
            "p50": percentile(latencies, 50),
            "p95": percentile(latencies, 95),
            "p99": percentile(latencies, 99),
        },
        "sample_failures": [
            {
                "status_code": r.status_code,
                "detail": r.error
                or (r.body.get("error") if isinstance(r.body, dict) else r.body),
            }
            for r in failed[:10]
        ],
        "raw_requests": [
            {"status_code": r.status_code, "elapsed_ms": r.elapsed_ms, "error": r.error}
            for r in results
        ],
    }


def print_load_report(load_report: dict):
    print("\n" + "=" * 60)
    print(
        f"Phase 2: load test — {load_report['total_requests']} requests, "
        f"concurrency={load_report['concurrency']}"
    )
    print("=" * 60)
    print(f"Wall time:           {load_report['wall_time_seconds']:.2f}s")
    print(f"Throughput:          {load_report['throughput_req_per_sec']:.1f} req/s")
    print(f"Succeeded (200):     {load_report['succeeded']}")
    print(f"Failed:              {load_report['failed']}")
    print("-" * 60)
    lat = load_report["latency_ms"]
    print(f"Latency min/avg/max: {lat['min']:.1f} / {lat['avg']:.1f} / {lat['max']:.1f} ms")
    print(f"Latency p50/p95/p99: {lat['p50']:.1f} / {lat['p95']:.1f} / {lat['p99']:.1f} ms")

    if load_report["sample_failures"]:
        print("-" * 60)
        print("Sample failures (up to 10):")
        for f in load_report["sample_failures"]:
            print(f"  status={f['status_code']}  detail={f['detail']}")
    print("=" * 60 + "\n")


async def run_load_test(
    client: httpx.AsyncClient, base_url: str, api_key: str, concurrency: int, sql: str
) -> dict:
    print(f"Firing {concurrency} concurrent requests: {sql!r}")
    start = time.perf_counter()
    tasks = [
        run_one_query(client, base_url, api_key, sql, []) for _ in range(concurrency)
    ]
    results = await asyncio.gather(*tasks)
    total_wall_time = time.perf_counter() - start
    load_report = build_load_report(results, total_wall_time, concurrency, sql)
    print_load_report(load_report)
    return load_report


# ---------------------------------------------------------------------
# Phase 3: server-side performance snapshot + JSON report assembly
# ---------------------------------------------------------------------


async def fetch_dashboard_snapshot(
    client: httpx.AsyncClient, base_url: str, dashboard_api_key: str
) -> dict:
    """Pull the server's own `/debug/performance` JSON right after the
    load test: per-stage trace timing, pool/gauge state, and recent
    request history straight from `core.performance`'s registry — the
    piece client-side wall-clock timing alone can't show (which stage
    inside the request actually cost the time). Requires an *admin*
    key (`install_performance_dashboard`'s `require_admin_user` gate),
    which the auto-registered load-test user is not, so this is opt-in
    via `--dashboard-api-key` rather than reusing `api_key`.
    """
    try:
        r = await client.get(
            f"{base_url}/debug/performance",
            headers={"x-api-key": dashboard_api_key},
        )
        if r.status_code != 200:
            return {
                "available": False,
                "reason": f"status={r.status_code} body={r.text[:300]}",
            }
        return {"available": True, "data": r.json()}
    except Exception as e:
        return {"available": False, "reason": str(e)}


def build_report(
    *,
    args: argparse.Namespace,
    smoke_results: Optional[list[CheckResult]],
    load_report: Optional[dict],
    dashboard_snapshot: Optional[dict],
) -> dict:
    """Assemble everything collected this run into one JSON-serializable dict."""
    report: dict = {
        "run_metadata": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "base_url": args.base_url,
            "concurrency": args.concurrency,
            "sql": args.sql,
            "smoke_only": args.smoke_only,
            "load_only": args.load_only,
        },
    }
    if smoke_results is not None:
        report["smoke_tests"] = {
            "checks": [asdict(r) for r in smoke_results],
            "passed": sum(1 for r in smoke_results if r.passed),
            "total": len(smoke_results),
        }
    if load_report is not None:
        if args.no_raw_requests:
            load_report = {k: v for k, v in load_report.items() if k != "raw_requests"}
        report["load_test"] = load_report
    if dashboard_snapshot is not None:
        report["server_performance_snapshot"] = dashboard_snapshot
    return report


def save_report(report: dict, output_path: Path) -> None:
    output_path.write_text(json.dumps(report, indent=2, default=str))
    print(f"Saved full report to: {output_path.resolve()}")


# ---------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------


async def main():
    parser = argparse.ArgumentParser(
        description="Verify the restructured API end to end, load test it, "
        "and save a JSON report for API-optimization analysis."
    )
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument(
        "--api-key", default=None, help="x-api-key to use. Omit if using --auto-auth."
    )
    parser.add_argument(
        "--auto-auth",
        action="store_true",
        default=True,
        help="Register + log in + mint a fresh API key before testing (default: on).",
    )
    parser.add_argument(
        "--username",
        default=None,
        help="Defaults to a unique per-run name to avoid register conflicts on repeat runs.",
    )
    parser.add_argument("--password", default="RestructureCheck123!")
    parser.add_argument(
        "--email",
        default=None,
        help="Defaults to a unique per-run address, same reasoning as --username.",
    )
    parser.add_argument(
        "--concurrency", type=int, default=500, help="Load test concurrency."
    )
    parser.add_argument(
        "--sql", default="SELECT 1 AS ok", help="SQL for the load test."
    )
    parser.add_argument(
        "--smoke-only", action="store_true", help="Run only the functional checks."
    )
    parser.add_argument(
        "--load-only", action="store_true", help="Run only the concurrent load test."
    )
    parser.add_argument(
        "--output",
        "-o",
        default=None,
        help="Path to save the JSON report. Defaults to "
        "load_test_report_<UTC timestamp>.json in the current directory.",
    )
    parser.add_argument(
        "--dashboard-api-key",
        default=None,
        help="Admin-scoped x-api-key. If given, GET /debug/performance is "
        "fetched after the load test and attached to the JSON report as "
        "'server_performance_snapshot' (per-stage timing, pool/gauge state). "
        "Requires PERF_ENABLED=1 on the server and an admin API key — see "
        "docs/performance/configuration.md and bootstrap_admin.py.",
    )
    parser.add_argument(
        "--no-raw-requests",
        action="store_true",
        help="Exclude the full per-request array from the saved JSON, keeping "
        "only aggregate stats (smaller file for very high concurrency runs).",
    )
    args = parser.parse_args()

    if not args.username or not args.email:
        suffix = secrets.token_hex(4)
        args.username = args.username or f"restructure_check_{suffix}"
        args.email = args.email or f"restructure_check_{suffix}@example.com"

    limits = httpx.Limits(
        max_connections=args.concurrency + 50,
        max_keepalive_connections=args.concurrency,
    )
    timeout = httpx.Timeout(60.0)

    smoke_results: Optional[list[CheckResult]] = None
    load_report: Optional[dict] = None
    dashboard_snapshot: Optional[dict] = None

    async with httpx.AsyncClient(limits=limits, timeout=timeout) as client:
        api_key = args.api_key
        user_id = None
        if not api_key or not args.load_only:
            print("Registering/logging in/minting API key...")
            api_key, user_id = await obtain_api_key(
                client, args.base_url, args.username, args.password, args.email
            )
            print(f"Using API key: {api_key}")

        if not api_key:
            raise SystemExit(
                "No API key available. Pass --api-key or leave --auto-auth on."
            )

        if not args.load_only:
            smoke_results = await run_smoke_tests(
                client, args.base_url, api_key, user_id or ""
            )
            print_smoke_report(smoke_results)

        if not args.smoke_only:
            load_report = await run_load_test(
                client, args.base_url, api_key, args.concurrency, args.sql
            )

        if args.dashboard_api_key:
            print("Fetching server-side /debug/performance snapshot...")
            dashboard_snapshot = await fetch_dashboard_snapshot(
                client, args.base_url, args.dashboard_api_key
            )
            if not dashboard_snapshot["available"]:
                print(f"  (unavailable: {dashboard_snapshot['reason']})")

    report = build_report(
        args=args,
        smoke_results=smoke_results,
        load_report=load_report,
        dashboard_snapshot=dashboard_snapshot,
    )
    if args.output:
        output_path = Path(args.output)
    else:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        output_path = Path(f"load_test_report_{ts}.json")
    save_report(report, output_path)


if __name__ == "__main__":
    asyncio.run(main())
