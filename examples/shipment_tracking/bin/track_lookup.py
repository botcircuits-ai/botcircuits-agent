#!/usr/bin/env python3
"""Per-item carrier lookup for the `shipment_tracking` listDecision.

The engine runs this once per tracking number (itemFacts kind=exec). It queries
the mock carrier API deterministically and prints a single flat JSON object of
facts that the workflow's `conditions` test. No LLM is involved.

Usage:
    track_lookup.py <tracking_number> [api_host] [delay_threshold_days]

Defaults keep the host and the delay threshold easy to change:
    api_host             http://localhost:4000/v1
    delay_threshold_days 7

A lookup that fails, times out, or returns a non-200/empty response yields
`lookup_failed: true` (-> outcome "error"); a 404 yields `not_found: true`
(-> outcome "not_found"). A single bad number never raises — it just produces an
error/not_found record for that one item, so the rest of the batch continues.

Output fields:
    tracking_number, status, last_location, estimated_delivery,
    lookup_failed (bool), not_found (bool),
    days_until_delivery (int|null), is_delayed (bool)
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime

_TIMEOUT_S = 10


def _days_until(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        d = datetime.strptime(iso, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None
    return (d - date.today()).days


def lookup(number: str, host: str, threshold: int) -> dict:
    base = {
        "tracking_number": number,
        "status": None,
        "last_location": None,
        "estimated_delivery": None,
        "lookup_failed": False,
        "not_found": False,
        "days_until_delivery": None,
        "is_delayed": False,
    }
    url = host.rstrip("/") + "/track?" + urllib.parse.urlencode({"number": number})
    try:
        with urllib.request.urlopen(url, timeout=_TIMEOUT_S) as resp:
            body = resp.read().decode("utf-8")
            data = json.loads(body) if body.strip() else None
    except urllib.error.HTTPError as exc:
        # 404 -> not_found; any other non-2xx -> generic lookup failure.
        if exc.code == 404:
            return {**base, "not_found": True}
        return {**base, "lookup_failed": True}
    except Exception:
        # timeout, connection refused, bad JSON, empty body, etc.
        return {**base, "lookup_failed": True}

    if not isinstance(data, dict) or not data.get("status"):
        return {**base, "lookup_failed": True}

    status = str(data.get("status"))
    eta = data.get("estimated_delivery")
    days = _days_until(eta)
    is_delayed = status == "in transit" and days is not None and days > threshold
    return {
        **base,
        "status": status,
        "last_location": data.get("last_location"),
        "estimated_delivery": eta,
        "days_until_delivery": days,
        "is_delayed": is_delayed,
    }


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(json.dumps({"lookup_failed": True}))
        return 0
    number = argv[1]
    host = argv[2] if len(argv) > 2 else "http://localhost:4000/v1"
    try:
        threshold = int(argv[3]) if len(argv) > 3 else 7
    except ValueError:
        threshold = 7
    print(json.dumps(lookup(number, host, threshold)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
