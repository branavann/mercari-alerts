#!/usr/bin/env python3
"""
Jobs the web UI asks GitHub Actions to run on its behalf.

The UI is a static page, so it cannot talk to Mercari. Instead it dispatches
a workflow, which runs one of these tasks and commits the result under
ui/data/, which the page then polls for.

    python tools/ui_task.py preview --id <request-id>
    python tools/ui_task.py learn   --id <request-id>

Input arrives through environment variables rather than argv so that
untrusted-looking JSON never touches a shell command line:

    TASK_PAYLOAD   JSON describing the request
"""

import argparse
import asyncio
import json
import os
import re
import sys
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mlert.config import Alert                                    # noqa: E402
from mlert.feed import CONDITION_LABELS, make_entry               # noqa: E402
from mlert.learn import Example, learn                            # noqa: E402
from mlert.mercari import (MercariClient, item_url, parse_item_id,  # noqa: E402
                           summary_fields)
from mlert.refine import card_terms, rule_tights                  # noqa: E402
from mlert.rules import evaluate                                  # noqa: E402

OUT_DIR = ROOT / "ui" / "data"
ID_RE = re.compile(r"^[A-Za-z0-9_-]{4,64}$")

PREVIEW_SAMPLE = 24
PREVIEW_DETAIL_BUDGET = 45
LEARN_MAX_EXAMPLES = 8


def _payload():
    raw = os.environ.get("TASK_PAYLOAD", "").strip()
    if not raw:
        raise SystemExit("TASK_PAYLOAD environment variable is empty")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise SystemExit(f"TASK_PAYLOAD is not valid JSON: {e}")


def _write(kind, request_id, doc):
    out = OUT_DIR / kind / f"{request_id}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    doc.setdefault("request_id", request_id)
    doc.setdefault("finished_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=1), "utf-8")
    print(f"wrote {out.relative_to(ROOT)}")
    return out


def _prune(kind, keep=40):
    """Old request results are disposable - don't let them pile up in git."""
    d = OUT_DIR / kind
    if not d.exists():
        return
    files = sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for stale in files[keep:]:
        try:
            stale.unlink()
        except OSError:
            pass


# --------------------------------------------------------------------------
# preview
# --------------------------------------------------------------------------

async def do_preview(request_id, payload):
    alert = Alert(payload["alert"], source="preview")
    client = MercariClient(cache_path=ROOT / ".term_counts.json")

    # Listings already dismissed in the panel. They stay out of the preview so
    # the headline count reflects what you would actually be shown.
    rejected_ids = {str(x) for x in (payload.get("rejected_ids")
                                     or alert.rejected_ids or []) if x}

    per_query, candidates = [], {}
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
            per_query.append({"query": q, "error": str(e)[:200]})
            continue
        added = 0
        for it in items:
            s = summary_fields(it)
            if s["id"] and s["id"] not in candidates and s["id"] not in rejected_ids:
                candidates[s["id"]] = s
                added += 1
        per_query.append({"query": q, "returned": len(items),
                          "total_on_mercari": num_found, "new_here": added})

    matched, borderline, budget = [], [], PREVIEW_DETAIL_BUDGET
    for item_id, s in candidates.items():
        desc = None
        v = evaluate(alert.rules, s["name"], None)
        if v.needs_description and budget > 0:
            try:
                full = await client.item(item_id)
                budget -= 1
                desc = getattr(full, "description", "") or ""
                v = evaluate(alert.rules, s["name"], desc)
            except Exception:
                desc = None
        rec = dict(s, score=v.score, hits=v.hits, _desc=desc)
        if v.status == "match":
            matched.append(rec)
        elif v.status == "borderline":
            borderline.append(rec)

    def card(rec, status):
        e = make_entry(alert, rec, status)
        e["condition"] = CONDITION_LABELS.get(rec.get("condition_id"))
        # What the panel needs to work out, on its own and without another
        # workflow run, which words separate the listings you reject from the
        # ones you keep. See mlert/refine.py.
        e.update(card_terms(rec.get("name"), rec.get("_desc")))
        return e

    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)

    def is_recent(rec):
        c = rec.get("created")
        if not c:
            return False
        try:
            dt = datetime.fromisoformat(str(c))
        except ValueError:
            return False
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone(timedelta(hours=9)))
        return dt >= cutoff

    matched.sort(key=lambda r: -(r.get("score") or 0))
    borderline.sort(key=lambda r: -(r.get("score") or 0))

    return {
        "status": "ok",
        "kind": "preview",
        "alert_name": alert.name,
        "queries": per_query,
        "candidates": len(candidates),
        "matched": len(matched),
        "matched_24h": sum(1 for r in matched if is_recent(r)),
        "borderline": len(borderline),
        "min_score": alert.rules.min_score,
        "detail_fetches_used": PREVIEW_DETAIL_BUDGET - budget,
        "hidden_rejected": len(rejected_ids),
        "current_tight": rule_tights(alert.rules),
        "sample": [card(r, "match") for r in matched[:PREVIEW_SAMPLE]],
        "near_misses": [card(r, "borderline") for r in borderline[:8]],
    }


# --------------------------------------------------------------------------
# learn from examples
# --------------------------------------------------------------------------

async def do_learn(request_id, payload):
    client = MercariClient(cache_path=ROOT / ".term_counts.json")

    async def fetch(refs):
        out = []
        for ref in refs[:LEARN_MAX_EXAMPLES]:
            item_id = parse_item_id(ref)
            if not item_id:
                continue
            item = await client.item(item_id)
            out.append(Example(
                name=getattr(item, "name", "") or "",
                description=getattr(item, "description", "") or "",
                item_id=item_id,
                price=getattr(item, "price", None),
            ))
        return out

    examples = await fetch(payload.get("examples") or [])
    if not examples:
        return {"status": "error", "kind": "learn",
                "error": "None of the example URLs could be fetched."}
    negatives = await fetch(payload.get("negatives") or [])

    counts = {}
    if payload.get("measure_specificity", True):
        from mlert.learn import (_collect_candidates, _drop_redundant,
                                 _rank_candidates)
        from mlert import textutil as tu

        cands = _collect_candidates(examples)
        _rank_candidates(list(cands.values()), len(examples))
        kept = [c for c in _drop_redundant(list(cands.values()))
                if c.df_all == len(examples) and not tu.is_stopword(c.surface)]
        kept.sort(key=lambda c: -c.rank)
        for c in kept[:14]:
            n = await client.count(c.surface)
            if n is not None:
                counts[c.surface] = n

    alert, rules, queries, report = learn(
        examples,
        negatives=negatives,
        counts=counts,
        name=payload.get("name") or "new-alert",
        label=payload.get("label"),
    )
    return {
        "status": "ok",
        "kind": "learn",
        "alert": alert,
        "report": report,
        "term_counts": counts,
        "examples": [
            {"id": ex.item_id, "name": ex.name, "price": ex.price,
             "url": item_url(ex.item_id)}
            for ex in examples
        ],
        "negatives": [
            {"id": ex.item_id, "name": ex.name, "url": item_url(ex.item_id)}
            for ex in negatives
        ],
    }


# --------------------------------------------------------------------------

TASKS = {"preview": do_preview, "learn": do_learn}
OUT_SUBDIR = {"preview": "previews", "learn": "learned"}


def main():
    p = argparse.ArgumentParser(description="Run a UI-requested job.")
    p.add_argument("task", choices=sorted(TASKS))
    p.add_argument("--id", required=True, help="request id (from the UI)")
    args = p.parse_args()

    if not ID_RE.match(args.id):
        raise SystemExit(f"refusing suspicious request id: {args.id!r}")

    try:
        payload = _payload()
        doc = asyncio.run(TASKS[args.task](args.id, payload))
    except SystemExit:
        raise
    except Exception as e:
        traceback.print_exc()
        doc = {"status": "error", "kind": args.task,
               "error": f"{type(e).__name__}: {e}"[:500]}

    sub = OUT_SUBDIR[args.task]
    _write(sub, args.id, doc)
    _prune(sub)
    # Always exit 0 - the error is reported in the result file the UI reads.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
