#!/usr/bin/env python3
"""
Build an alert from example listings.

    python learn_alert.py --name op-hyperbattle-ce2 \
        --label "One Piece Hyper Battle C-E2" \
        https://jp.mercari.com/item/m64572484257 \
        https://jp.mercari.com/item/m83971401198 \
        https://jp.mercari.com/item/m89240778353 \
        https://jp.mercari.com/item/m45169992781

Add listings you do NOT want with --not, which is how the tool learns
exclusions and how it works out which words merely identify the product
family rather than the specific item:

        --not https://jp.mercari.com/item/mXXXXXXXXXX

It prints a coverage + scoring report and the YAML block. Nothing is written
until you pass --write, and even then you should read the report first.

Two or three examples is enough to start; more (and more differently-worded)
examples make it better. Sold listings work fine — the page stays up.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from mlert import textutil as tu
from mlert.config import append_alert
from mlert.learn import Example, learn, to_yaml_block
from mlert.mercari import MercariClient, parse_item_id

ROOT = Path(__file__).resolve().parent
ALERTS_FILE = ROOT / "alerts.yaml"
COUNT_CACHE = ROOT / ".term_counts.json"

# Cap how many terms we ask Mercari to count, so learning stays a handful of
# seconds rather than a minute.
MAX_TERMS_TO_COUNT = 14


async def fetch_examples(client, refs):
    out = []
    for ref in refs:
        item_id = parse_item_id(ref)
        print(f"  fetching {item_id} ...", file=sys.stderr)
        item = await client.item(item_id)
        out.append(
            Example(
                name=getattr(item, "name", "") or "",
                description=getattr(item, "description", "") or "",
                item_id=item_id,
                price=getattr(item, "price", None),
            )
        )
    return out


async def measure_counts(client, examples, limit=MAX_TERMS_TO_COUNT):
    """
    Ask Mercari how many live listings each candidate term matches. This is
    the difference between 'ワンピース' (a product family) and
    'ゴーイング・メリー号' (one specific card).
    """
    from mlert.learn import _collect_candidates, _drop_redundant, _rank_candidates

    cands = _collect_candidates(examples)
    _rank_candidates(list(cands.values()), len(examples))
    kept = [c for c in _drop_redundant(list(cands.values()))
            if c.df_all == len(examples) and not tu.is_stopword(c.surface)]
    kept.sort(key=lambda c: -c.rank)

    counts = {}
    for c in kept[:limit]:
        n = await client.count(c.surface)
        if n is not None:
            counts[c.surface] = n
            print(f"  {c.surface:<24} {n:>9,} listings", file=sys.stderr)
    return counts


async def run(args):
    client = MercariClient(cache_path=COUNT_CACHE)

    print("Fetching example listings...", file=sys.stderr)
    examples = await fetch_examples(client, args.urls)
    negatives = await fetch_examples(client, args.negative) if args.negative else []

    counts = {}
    if not args.no_idf:
        print("\nMeasuring how common each term is on Mercari...", file=sys.stderr)
        counts = await measure_counts(client, examples)

    alert, rules, queries, report = learn(
        examples,
        negatives=negatives,
        counts=counts,
        name=args.name,
        label=args.label,
        max_queries=args.max_queries,
    )
    if args.price_min is not None:
        alert["price_min"] = args.price_min
    if args.price_max is not None:
        alert["price_max"] = args.price_max

    print()
    print("=" * 72)
    print(report)
    print("=" * 72)
    if not counts:
        print("\nNOTE: term-specificity measurement was skipped, so common words like")
        print("      'ワンピース' carry the same weight as rare ones. Expect more noise.")
    print()
    print(to_yaml_block(alert))

    if args.write:
        append_alert(ALERTS_FILE, alert)
        print(f"Written to {ALERTS_FILE.name} as '{alert['name']}'.")
        print("Next: python mercari_alert.py --dry-run --only " + alert["name"])
    else:
        print("Not written. Re-run with --write to add it to alerts.yaml,")
        print("or paste the block above in yourself.")
    return 0


def main():
    p = argparse.ArgumentParser(
        description="Learn an alert's search strategy from example listings.")
    p.add_argument("urls", nargs="+", help="Mercari item URLs or ids of the item you want")
    p.add_argument("--not", dest="negative", action="append", default=[],
                   metavar="URL", help="a listing you do NOT want (repeatable)")
    p.add_argument("--name", required=True, help="short id for the alert, e.g. op-ce2")
    p.add_argument("--label", help="human-readable name shown in emails")
    p.add_argument("--price-min", type=int)
    p.add_argument("--price-max", type=int)
    p.add_argument("--max-queries", type=int, default=8)
    p.add_argument("--no-idf", action="store_true",
                   help="skip measuring term rarity (faster, noticeably worse)")
    p.add_argument("--write", action="store_true", help="append the result to alerts.yaml")
    args = p.parse_args()
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
