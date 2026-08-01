#!/usr/bin/env python3
"""
Post-restructure verification for the PrepareData API.

Two phases:
  1. Functional smoke tests — one request per route, checking each piece
     touched by the restructure actually still works end to end (auth
     bootstrap, health, query, tables, the new API-key validation cache).
  2. The original concurrent load test (unchanged behavior), so you can
     confirm throughput/correctness didn't regress alongside the
     structural changes.

Requires: httpx   ->   pip install httpx

Usage:
    python load_test.py                       # both phases, --auto-auth
    python load_test.py --smoke-only           # just phase 1
    python load_test.py --load-only --api-key YOUR_KEY
    python load_test.py --concurrency 200 --sql "SELECT 1"
"""

import argparse
import asyncio
import secrets
import statistics
import time
from dataclasses import dataclass
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


def print_load_report(results: list, total_wall_time: float, concurrency: int):
    latencies = [r.elapsed_ms for r in results]
    ok = [r for r in results if r.status_code == 200 and not r.error]
    failed = [r for r in results if r not in ok]

    print("\n" + "=" * 60)
    print(f"Phase 2: load test — {len(results)} requests, concurrency={concurrency}")
    print("=" * 60)
    print(f"Wall time:           {total_wall_time:.2f}s")
    print(f"Throughput:          {len(results) / total_wall_time:.1f} req/s")
    print(f"Succeeded (200):     {len(ok)}")
    print(f"Failed:              {len(failed)}")
    print("-" * 60)
    print(
        f"Latency min/avg/max: {min(latencies):.1f} / "
        f"{statistics.mean(latencies):.1f} / {max(latencies):.1f} ms"
    )
    print(
        f"Latency p50/p95/p99: {percentile(latencies, 50):.1f} / "
        f"{percentile(latencies, 95):.1f} / {percentile(latencies, 99):.1f} ms"
    )

    if failed:
        print("-" * 60)
        print("Sample failures (up to 10):")
        for r in failed[:10]:
            detail = r.error or (
                r.body.get("error") if isinstance(r.body, dict) else r.body
            )
            print(f"  status={r.status_code}  detail={detail}")
    print("=" * 60 + "\n")


async def run_load_test(
    client: httpx.AsyncClient, base_url: str, api_key: str, concurrency: int, sql: str
):
    print(f"Firing {concurrency} concurrent requests: {sql!r}")
    start = time.perf_counter()
    tasks = [
        run_one_query(client, base_url, api_key, sql, []) for _ in range(concurrency)
    ]
    results = await asyncio.gather(*tasks)
    total_wall_time = time.perf_counter() - start
    print_load_report(results, total_wall_time, concurrency)


# ---------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------


async def main():
    parser = argparse.ArgumentParser(
        description="Verify the restructured API end to end, then load test it."
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
            if args.smoke_only:
                return

        if not args.smoke_only:
            await run_load_test(
                client, args.base_url, api_key, args.concurrency, args.sql
            )


if __name__ == "__main__":
    asyncio.run(main())
