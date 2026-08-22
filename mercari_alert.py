#!/usr/bin/env python3
"""
Mercari Japan alert runner.

For each alert in alerts.yaml:
  1. run every query in its fan-out (recall)
  2. drop anything already evaluated on a previous run
  3. score what's left against the alert's match rules, fetching the full
     description only when the title alone can't decide (precision)
  4. email a digest of confident matches, with near-misses in a second section

Run modes:
    python mercari_alert.py                 normal scheduled run
    python mercari_alert.py --dry-run       evaluate + print, send nothing,
                                            save nothing
    python mercari_alert.py --only NAME     just one alert
    python mercari_alert.py --explain NAME  show the score breakdown per listing
"""

import argparse
import asyncio
import sys
from pathlib import Path

from mlert import notify
from mlert.config import load_alerts
from mlert.mercari import MercariClient, item_url, summary_fields
from mlert.rules import evaluate
from mlert.state import State

ROOT = Path(__file__).resolve().parent
ALERTS_FILE = ROOT / "alerts.yaml"
STATE_FILE = ROOT / "seen_state.json"

# Fetching a description is one API call. We only ever do it for listings we
# have never evaluated before, so in steady state this stays small.
MAX_DETAIL_FETCHES = 80


async def gather_candidates(client, alert, verbose=True):
    """Run the query fan-out and return {item_id: summary_dict}."""
    found = {}
    for q in alert.queries:
        try:
            items, num_found = await client.search(
                q,
                price_min=alert.price_min,
                price_max=alert.price_max,
                categories=alert.categories,
                exclude=alert.mercari_exclude,
                limit=alert.per_query_limit,
            )
        except Exception as e:
            print(f"    query {q!r} failed: {e}", file=sys.stderr)
            continue
        new_here, no_id = 0, 0
        for it in items:
            s = summary_fields(it)
            if not s["id"]:
                no_id += 1
                continue
            if s["id"] not in found:
                found[s["id"]] = s
                new_here += 1
        if verbose:
            total = f"{num_found:,}" if isinstance(num_found, int) else "?"
            print(f"    {q!r}: {len(items)} returned ({total} total), {new_here} new to this run")
        if no_id:
            print(f"    WARNING: {no_id}/{len(items)} results for {q!r} had no extractable "
                  f"item id (see the DEBUG item sample above/below for the real field names) "
                  f"and were skipped", file=sys.stderr)
    return found


async def evaluate_candidates(client, alert, candidates, budget, explain=False):
    """
    Score candidates, fetching descriptions only where it can change the
    outcome. Returns (matches, borderlines, fetches_used).
    """
    matches, borderlines, used = [], [], 0

    for item_id, s in candidates.items():
        v = evaluate(alert.rules, s["name"], None)

        if v.needs_description and used < budget:
            try:
                full = await client.item(item_id)
                used += 1
                desc = getattr(full, "description", "") or ""
                v = evaluate(alert.rules, s["name"], desc)
            except Exception as e:
                print(f"    detail fetch failed for {item_id}: {e}", file=sys.stderr)

        if explain:
            print(f"    [{v.status:<10} {v.score:>7}] {s['name'][:70]}")
            if v.excluded_by:
                print(f"                     excluded by: {v.excluded_by}")
            elif v.hits:
                print(f"                     matched: {', '.join(v.hits[:8])}")

        rec = dict(s, score=v.score, hits=v.hits)
        if v.status == "match":
            matches.append(rec)
        elif v.status == "borderline":
            borderlines.append(rec)

    return matches, borderlines, used


async def run(args):
    alerts = load_alerts(ALERTS_FILE)
    if args.only:
        alerts = [a for a in alerts if a.name == args.only]
        if not alerts:
            print(f"No alert named {args.only!r} in alerts.yaml", file=sys.stderr)
            return 1
    if not alerts:
        print("No alerts configured — nothing to do.")
        return 0

    state = State(STATE_FILE)
    client = MercariClient(cache_path=ROOT / ".term_counts.json")

    sections, borderline_sections, stats = {}, {}, {}
    detail_budget = MAX_DETAIL_FETCHES
    errors = []

    for alert in alerts:
        if alert.paused:
            print(f"[{alert.name}] paused, skipping")
            continue
        if not args.dry_run and not state.due(alert.name, alert.min_interval_minutes):
            print(f"[{alert.name}] checked recently, skipping")
            continue

        print(f"[{alert.name}] {len(alert.queries)} queries")
        try:
            candidates = await gather_candidates(client, alert)
        except Exception as e:
            print(f"  ERROR: {e}", file=sys.stderr)
            errors.append((alert.name, str(e)))
            continue

        first_run = state.is_first_run(alert.name)
        already = state.seen(alert.name)
        fresh = {k: v for k, v in candidates.items() if k not in already}
        print(f"  {len(candidates)} candidates, {len(fresh)} not seen before")

        if first_run and not args.dry_run:
            state.record_seen(alert.name, list(candidates.keys()))
            print(f"  first run — baselined {len(candidates)} listings, no email")
            continue

        to_check = fresh if not args.dry_run else candidates
        matches, borderlines, used = await evaluate_candidates(
            client, alert, to_check, detail_budget, explain=args.explain
        )
        detail_budget -= used
        if detail_budget <= 0:
            print("  note: hit the per-run description-fetch budget; "
                  "remaining listings deferred to the next run", file=sys.stderr)

        # relist detection + price stats
        median = state.median_price(alert.name)
        kept = []
        for m in matches:
            fp = State.fingerprint(m["name"], m.get("price"), m.get("seller_id"))
            m["relist"] = state.seen_fingerprint(fp)
            if m["relist"] and alert.suppress_relists:
                continue
            if not args.dry_run:
                state.record_fingerprint(fp, m["id"])
                state.record_price(alert.name, m.get("price"))
            if median and isinstance(m.get("price"), (int, float)):
                m["deal"] = m["price"] <= median * 0.75
            kept.append(m)

        if kept:
            sections[alert.label] = kept
            stats[alert.label] = {"median": median}
        if borderlines:
            borderline_sections[alert.label] = borderlines[:8]

        print(f"  -> {len(kept)} match, {len(borderlines)} borderline")
        for m in kept:
            print(f"     MATCH {m['score']:>7}  {item_url(m['id'])}  {m['name'][:60]}")

        if not args.dry_run:
            state.record_seen(alert.name, list(candidates.keys()))

    if args.dry_run:
        print("\n(dry run — no email sent, no state saved)")
        return 0

    state.save()

    if sections or borderline_sections:
        subject, text, html_body = notify.compose(sections, borderline_sections, stats)
        try:
            to = notify.send(subject, text, html_body)
            print(f"\nEmailed {to}: {subject}")
        except Exception as e:
            print(f"ERROR sending email: {e}", file=sys.stderr)
            return 1
    else:
        print("\nNothing new.")

    if errors:
        print(f"Completed with {len(errors)} alert error(s): {errors}", file=sys.stderr)
    return 0


def main():
    p = argparse.ArgumentParser(description="Check Mercari Japan for new listings.")
    p.add_argument("--dry-run", action="store_true",
                   help="evaluate everything currently listed, print results, "
                        "send no email and write no state")
    p.add_argument("--only", metavar="NAME", help="run just one alert by name")
    p.add_argument("--explain", action="store_true",
                   help="print a score breakdown for every candidate")
    args = p.parse_args()
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
