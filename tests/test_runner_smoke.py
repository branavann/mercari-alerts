"""
End-to-end smoke test of the runner with a stubbed Mercari API.

Verifies the orchestration that can't be checked offline any other way:
first-run baselining, only-evaluate-new-listings, description fetching only
when needed, relist suppression, and email composition.

    python tests/test_runner_smoke.py
"""

import asyncio
import json
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# --- stub out the `mercapi` package before anything imports it -------------
fake = types.ModuleType("mercapi")


class _FakeMercapi:
    pass


fake.Mercapi = _FakeMercapi
sys.modules.setdefault("mercapi", fake)

from mlert import notify                                    # noqa: E402
from mlert.config import Alert                              # noqa: E402
from mlert.state import State                               # noqa: E402
import mercari_alert                                        # noqa: E402

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "onepiece_ce2.json").read_text("utf-8"))
LISTINGS = {e["id"]: e for e in FIXTURE["examples"] + FIXTURE["negatives"]}


class FakeItem:
    def __init__(self, rec, with_desc=False):
        self.id = rec["id"]
        self.name = rec["name"]
        self.price = rec["price"]
        self.thumbnails = [f"https://example.invalid/{rec['id']}.jpg"]
        self.seller_id = "seller-A"
        if with_desc:
            self.description = rec["description"]


class FakeClient:
    """Returns every fixture listing for any query; counts API calls."""

    def __init__(self, ids=None):
        self.ids = ids or list(LISTINGS)
        self.searches = 0
        self.detail_fetches = 0

    async def search(self, query, **kw):
        self.searches += 1
        return [FakeItem(LISTINGS[i]) for i in self.ids], 1234

    async def item(self, item_id):
        self.detail_fetches += 1
        return FakeItem(LISTINGS[item_id], with_desc=True)


ALERT_DICT = {
    "name": "op-ce2",
    "label": "One Piece C-E2",
    "queries": ["C-E2", "ゴーイング・メリー号 ルフィ海賊団"],
    "match": {
        "require": [["ワンピース", "ルフィ海賊団", "ゴーイング・メリー号"]],
        "signals": {
            "C-E2": 13.0, "ゴーイング・メリー号": 12.7, "ルフィ海賊団": 11.6,
            "ハイパーバトル": 9.1, "出航": 7.6, "カードダス": 2.0, "ワンピース": 0.4,
        },
        "exclude": ["ノーマル"],
        "min_score": 19.3,
        "scope": "full",
    },
}


def _args(**kw):
    ns = types.SimpleNamespace(dry_run=False, only=None, explain=False)
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


def run_once(state, client, alert, args=None):
    args = args or _args()
    return asyncio.run(_run_one(state, client, alert, args))


async def _run_one(state, client, alert, args):
    candidates = await mercari_alert.gather_candidates(client, alert, verbose=False)
    first = state.is_first_run(alert.name)
    fresh = {k: v for k, v in candidates.items() if k not in state.seen(alert.name)}
    if first:
        state.record_seen(alert.name, list(candidates))
        return {"first_run": True, "matches": [], "borderline": []}
    m, b, _used = await mercari_alert.evaluate_candidates(client, alert, fresh, 80)
    state.record_seen(alert.name, list(candidates))
    return {"first_run": False, "matches": m, "borderline": b}


def main():
    failures = []

    def check(cond, msg):
        print(("  PASS  " if cond else "  FAIL  ") + msg)
        if not cond:
            failures.append(msg)

    with tempfile.TemporaryDirectory() as td:
        alert = Alert(ALERT_DICT)
        state = State(Path(td) / "state.json")

        # 1. first run baselines and emails nothing
        c1 = FakeClient()
        r1 = run_once(state, c1, alert)
        check(r1["first_run"] and not r1["matches"],
              "first run baselines without emailing")
        check(c1.detail_fetches == 0, "first run fetches no descriptions")

        # 2. second run with the same listings finds nothing new
        c2 = FakeClient()
        r2 = run_once(state, c2, alert)
        check(not r2["matches"] and not r2["borderline"],
              "unchanged listings produce no alerts on the next run")

        # 3. a genuinely new listing is caught
        LISTINGS["m_new_001"] = {
            "id": "m_new_001", "price": 88000,
            "name": "ワンピース ハイパーバトル C-E2 ルフィ海賊団 出航",
            "description": "ゴーイング・メリー号 カードダス プロモ",
        }
        c3 = FakeClient(ids=list(LISTINGS))
        r3 = run_once(state, c3, alert)
        check([m["id"] for m in r3["matches"]] == ["m_new_001"],
              "a new matching listing is detected exactly once")
        check(c3.detail_fetches <= 2,
              f"descriptions fetched only when needed (was {c3.detail_fetches})")

        # 4. the same item relisted under a new id is flagged
        fp = State.fingerprint(LISTINGS["m_new_001"]["name"], 88000, "seller-A")
        state.record_fingerprint(fp, "m_new_001")
        LISTINGS["m_new_002"] = dict(LISTINGS["m_new_001"], id="m_new_002", price=88200)
        c4 = FakeClient(ids=list(LISTINGS))
        r4 = run_once(state, c4, alert)
        got = r4["matches"]
        check(len(got) == 1 and got[0]["id"] == "m_new_002", "relisted item is picked up")
        check(State.fingerprint(got[0]["name"], got[0]["price"], "seller-A") == fp,
              "relisted item fingerprints to the original")

        # 5. negatives never match
        neg_ids = {n["id"] for n in FIXTURE["negatives"]}
        all_ids = {m["id"] for m in r3["matches"] + r4["matches"]}
        check(not (all_ids & neg_ids), "negative fixtures never alert")

        # 6. email composition
        subject, text, html_body = notify.compose(
            {"One Piece C-E2": r4["matches"]},
            {"One Piece C-E2": r4["borderline"][:2]},
            {"One Piece C-E2": {"median": 150000}},
        )
        check("Mercari" in subject and "One Piece C-E2" in subject, "subject line is informative")
        check("jp.mercari.com/item/m_new_002" in html_body, "email links to the listing")
        check("jp.mercari.com/item/m_new_002" in text, "plain-text part links too")
        check(html_body.count("<table") >= 1, "email renders listing cards")

    print(f"\n{'ALL PASSED' if not failures else str(len(failures)) + ' FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
