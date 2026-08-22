#!/usr/bin/env python3
"""
Price comps for an alert: what have matching items actually SOLD for?

    python comps.py op-hyperbattle-ce2

Runs the alert's own queries against Mercari's sold listings and applies the
same match rules, so the prices you get are for the item you actually want -
not for everything that happens to share a keyword. Use it to decide what a
fair price is, and to set a sensible price_max so you stop being alerted
about listings priced far above market.
"""

import argparse
import asyncio
import statistics
import sys
from pathlib import Path

from mlert.config import load_alerts
from mlert.mercari import MercariClient, item_url, summary_fields
from mlert.rules import evaluate

ROOT = Path(__file__).resolve().parent
ALERTS_FILE = ROOT / "alerts.yaml"


async def run(args):
    alerts = {a.name: a for a in load_alerts(ALERTS_FILE)}
    alert = alerts.get(args.alert)
    if not alert:
        print(f"No alert named {args.alert!r}. Available: {', '.join(alerts)}", file=sys.stderr)
        return 1

    client = MercariClient(cache_path=ROOT / ".term_counts.json")
    seen = {}
    for q in alert.queries:
        try:
            items, _n = await client.search(
                q, sold=True, categories=alert.categories, limit=args.limit
            )
        except Exception as e:
            print(f"  query {q!r} failed: {e}", file=sys.stderr)
            continue
        for it in items:
            s = summary_fields(it)
            if s["id"]:
                seen.setdefault(s["id"], s)

    print(f"{len(seen)} sold candidates from {len(alert.queries)} queries; scoring...")

    matched, budget = [], args.max_fetches
    for item_id, s in seen.items():
        v = evaluate(alert.rules, s["name"], None)
        if v.needs_description and budget > 0:
            try:
                full = await client.item(item_id)
                budget -= 1
                v = evaluate(alert.rules, s["name"], getattr(full, "description", "") or "")
            except Exception:
                pass
        if v.matched and isinstance(s.get("price"), (int, float)):
            matched.append(s)

    if not matched:
        print("No confirmed sold matches. Try --limit higher, or loosen the rules.")
        return 0

    prices = sorted(m["price"] for m in matched)
    print(f"\n{len(prices)} confirmed sold listings for '{alert.label}'")
    print(f"  min     ¥{prices[0]:,}")
    print(f"  median  ¥{int(statistics.median(prices)):,}")
    print(f"  mean    ¥{int(statistics.mean(prices)):,}")
    print(f"  max     ¥{prices[-1]:,}")
    print("\nRecent sold:")
    for m in matched[: args.show]:
        print(f"  ¥{m['price']:>9,}  {item_url(m['id'])}  {m['name'][:56]}")

    print(f"\nSuggested alert setting:  price_max: {int(statistics.median(prices) * 1.15)}")
    return 0


def main():
    p = argparse.ArgumentParser(description="Sold-price comps for one alert.")
    p.add_argument("alert", help="alert name from alerts.yaml")
    p.add_argument("--limit", type=int, default=40, help="results per query")
    p.add_argument("--max-fetches", type=int, default=60, help="description fetch budget")
    p.add_argument("--show", type=int, default=12, help="how many sold listings to print")
    args = p.parse_args()
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
