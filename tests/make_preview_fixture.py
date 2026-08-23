#!/usr/bin/env python3
"""
Build a realistic preview result for the browser test, using the same code
path the live preview task uses (mlert.refine.card_terms). Keeping the
fixture generated rather than hand-written means the browser test can never
drift away from what tools/ui_task.py actually emits.

    python tests/make_preview_fixture.py > tests/fixtures/preview_result.json
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mlert.refine import card_terms, rule_tights   # noqa: E402
from mlert.rules import MatchRules                 # noqa: E402

# Six listings from a real preview run. Two are the card being hunted
# (冒険を求めて); the rest are different cards from the same series that score
# identically, because every one of them contains 冒険.
LISTINGS = [
    ("m1", "【限定品】ワンピース ハイパーバトル カードダス ルフィ海賊団 冒険を求めて",
     140000, 30.5, True),
    ("m2", "ワンピースカード　ハイパーバトル ルフィ海賊団 冒険を求めて　特典　CJ1",
     90500, 29.25, True),
    ("m3", "ワンピースカードダス　ハイパーバトル まとめ300枚　ルフィ海賊団　冒険を求めて",
     299999, 30.5, False),
    ("m4", "ワンピース カードダスハイパーバトル ルフィ海賊団 冒険の夜明け",
     5000, 30.5, False),
    ("m5", "ワンピース カードゲーム ルフィ海賊団 冒険の夜明け", 34000, 25.5, False),
    ("m6", "ワンピース カードゲーム ルフィ海賊団 虹の島を目指す冒険者", 10000, 25.5, False),
]

RULES = MatchRules(
    require=[["ワンピース", "ONE PIECE"]],
    signals={"ルフィ海賊団": 6.0, "ハイパーバトル": 5.0, "カードダス": 4.0,
             "ワンピース": 4.0, "冒険": 4.0},
    exclude=["複製"], min_score=18.0,
)


def main():
    sample = []
    for item_id, name, price, score, _wanted in LISTINGS:
        card = {
            "id": item_id, "url": f"https://jp.mercari.com/item/{item_id}",
            "marketplace": "mercari", "alert": "op-ce2", "alert_label": "One Piece C-E2",
            "name": name, "price": price, "thumbnail": None, "condition": "Fair",
            "detected_at": "2026-08-23T00:00:00+00:00", "score": score,
            "hits": ["ルフィ海賊団", "ワンピース", "冒険", "ハイパーバトル"],
            "status": "match", "deal": False, "relist": False, "target_hit": False,
            "lag_seconds": 36000,
        }
        card.update(card_terms(name))
        sample.append(card)

    json.dump({
        "status": "ok", "kind": "preview", "request_id": "testreq",
        "alert_name": "op-ce2",
        "queries": [{"query": "ハイパーバトル ルフィ海賊団", "returned": 30,
                     "total_on_mercari": 1911, "new_here": 6}],
        "candidates": 6, "matched": len(sample), "matched_24h": 1, "borderline": 0,
        "min_score": RULES.min_score, "detail_fetches_used": 0, "hidden_rejected": 0,
        "current_tight": rule_tights(RULES),
        "sample": sample, "near_misses": [],
    }, sys.stdout, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
