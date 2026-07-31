#!/usr/bin/env python3
"""Run a bounded HTTP load test against a bminfo deployment.

The script uses only the Python standard library. It deliberately accepts the
test password through an environment variable so credentials are not stored in
the repository or echoed in the results.

Example:

    LOAD_TEST_LOGIN=EA7KLK LOAD_TEST_PASSWORD='...' \
      python scripts/load_test.py --base-url https://bminfo.ea7klk.es
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import http.cookiejar
import json
import os
from statistics import mean
import time
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener


DEFAULT_BASE_URL = "https://bminfo.ea7klk.es"
DEFAULT_REQUESTS = 250
DEFAULT_CONCURRENCY = 25
TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class Result:
    path: str
    requests: int
    concurrency: int
    errors: int
    statuses: dict[str, int]
    requests_per_second: float
    p50_ms: float
    p95_ms: float
    p99_ms: float
    max_ms: float
    average_ms: float
    bytes_received: int


def percentile(values: list[float], fraction: float) -> float:
    index = min(len(values) - 1, max(0, int(len(values) * fraction + 0.999999) - 1))
    return values[index]


def request_once(base_url: str, path: str, cookie: str | None) -> tuple[float, int, int]:
    request = Request(
        f"{base_url}{path}",
        headers={
            "Accept": "application/json, text/html, application/pdf",
            "User-Agent": "bminfo-load-test/1.0",
            **({"Cookie": cookie} if cookie else {}),
        },
    )
    started = time.perf_counter()
    try:
        with build_opener().open(request, timeout=TIMEOUT_SECONDS) as response:
            body = response.read()
            status = response.status
    except HTTPError as error:
        body = error.read()
        status = error.code
    except (OSError, URLError):
        body = b""
        status = 0
    elapsed_ms = (time.perf_counter() - started) * 1000
    return elapsed_ms, status, len(body)


def benchmark(
    base_url: str,
    path: str,
    cookie: str | None,
    requests: int,
    concurrency: int,
) -> Result:
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [executor.submit(request_once, base_url, path, cookie) for _ in range(requests)]
        samples = [future.result() for future in futures]
    elapsed = max(time.perf_counter() - started, 0.000001)
    timings = sorted(sample[0] for sample in samples)
    statuses: dict[str, int] = {}
    for _, status, _ in samples:
        statuses[str(status)] = statuses.get(str(status), 0) + 1
    errors = sum(count for status, count in statuses.items() if status == "0" or not status.startswith("2"))
    return Result(
        path=path,
        requests=requests,
        concurrency=concurrency,
        errors=errors,
        statuses=statuses,
        requests_per_second=requests / elapsed,
        p50_ms=percentile(timings, 0.50),
        p95_ms=percentile(timings, 0.95),
        p99_ms=percentile(timings, 0.99),
        max_ms=timings[-1],
        average_ms=mean(timings),
        bytes_received=sum(sample[2] for sample in samples),
    )


def login(base_url: str, login_name: str, password: str) -> str:
    jar = http.cookiejar.CookieJar()
    opener = build_opener(HTTPCookieProcessor(jar))
    body = urlencode({"login": login_name, "password": password}).encode()
    request = Request(
        f"{base_url}/user/login",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "bminfo-load-test/1.0",
        },
    )
    with opener.open(request, timeout=TIMEOUT_SECONDS):
        pass
    for cookie in jar:
        if cookie.name == "session_token":
            return f"session_token={cookie.value}"
    raise RuntimeError("login did not produce a session cookie")


def paths(include_authenticated: bool) -> Iterable[tuple[str, bool]]:
    yield "/", False
    yield "/health", False
    yield "/api/stats/summary", False
    yield "/public/stats?timeRange=24h&continent=Europe&country=ES", False
    yield "/public/lastheard?limit=50", False
    if include_authenticated:
        yield "/user/api/stats", True
        yield "/user/live-qsos/data?rows=25&timeRange=30m", True
        yield "/user/reports?timeRange=24h", True
        yield "/user/reports/export.pdf?timeRange=24h", True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=os.getenv("LOAD_TEST_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--login", default=os.getenv("LOAD_TEST_LOGIN"))
    parser.add_argument("--password", default=os.getenv("LOAD_TEST_PASSWORD"))
    parser.add_argument("--requests", type=int, default=DEFAULT_REQUESTS)
    parser.add_argument(
        "--concurrency",
        type=int,
        default=int(os.getenv("LOAD_TEST_CONCURRENCY", str(DEFAULT_CONCURRENCY))),
        help="number of concurrent clients (also configurable with LOAD_TEST_CONCURRENCY)",
    )
    return parser.parse_args()


def main() -> int:
    arguments = parse_args()
    if arguments.requests < 1 or arguments.concurrency < 1:
        raise SystemExit("--requests and --concurrency must be positive")
    base_url = arguments.base_url.rstrip("/")
    cookie = None
    if arguments.login or arguments.password:
        if not arguments.login or not arguments.password:
            raise SystemExit("--login and --password must be supplied together")
        cookie = login(base_url, arguments.login, arguments.password)

    for path, authenticated in paths(cookie is not None):
        result = benchmark(base_url, path, cookie if authenticated else None, arguments.requests, arguments.concurrency)
        print(json.dumps({
            "base_url": base_url,
            "path": result.path,
            "requests": result.requests,
            "concurrency": result.concurrency,
            "errors": result.errors,
            "statuses": result.statuses,
            "requests_per_second": round(result.requests_per_second, 2),
            "average_ms": round(result.average_ms, 2),
            "p50_ms": round(result.p50_ms, 2),
            "p95_ms": round(result.p95_ms, 2),
            "p99_ms": round(result.p99_ms, 2),
            "max_ms": round(result.max_ms, 2),
            "bytes_received": result.bytes_received,
        }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
