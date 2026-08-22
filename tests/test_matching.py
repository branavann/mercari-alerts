"""
Offline tests for the matching engine and the learner.

No network: everything runs against tests/fixtures/onepiece_ce2.json, which
holds four REAL mercari.jp listings of the same card plus three near-miss
listings. Run with:  python -m pytest tests/ -q     (or: python tests/test_matching.py)
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mlert import textutil as tu                      # noqa: E402
from mlert.learn import Example, idf_factor, learn    # noqa: E402
from mlert.rules import MatchRules, evaluate          # noqa: E402
from mlert.state import State                         # noqa: E402

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "onepiece_ce2.json").read_text("utf-8")
)
EXAMPLES = [Example(e["name"], e["description"], e["id"], e["price"])
            for e in FIXTURE["examples"]]
NEGATIVES = [Example(e["name"], e["description"], e["id"], e["price"])
             for e in FIXTURE["negatives"]]

# Stand-in for live Mercari result counts so the tests stay offline.
COUNTS = {
    "ワンピース": 480000, "カードダス": 21000, "ハイパーバトル": 900,
    "ルフィ海賊団": 140, "ゴーイング・メリー号": 60, "出航": 3000,
    "C-E2": 45, "プロモ": 260000, "カードゲーム": 90000, "2000年": 40000,
}


# --------------------------------------------------------------------------
# normalisation
# --------------------------------------------------------------------------

def test_code_variants_normalise_together():
    forms = ["C-E2", "CE-2", "CE2", "ｃ－ｅ２", "C‐E2"]
    assert len({tu.tight(f) for f in forms}) == 1, [tu.tight(f) for f in forms]


def test_katakana_separator_variants_normalise_together():
    assert tu.tight("ゴーイング・メリー号") == tu.tight("ゴーイングメリー号")
    assert tu.tight("ワンピース") == tu.tight("わんぴーす")


def test_prolonged_sound_mark_is_preserved():
    # Stripping ー would wrongly merge these two distinct words.
    assert tu.tight("ビル") != tu.tight("ビール")


def test_short_code_respects_boundaries():
    text = tu.tight("型番 AF6261 の商品")
    assert not tu.contains("F626", text)
    assert tu.contains("F626", tu.tight("メリー号　F626"))


def test_script_run_segmentation():
    terms = set(tu.extract_terms("ワンピース 旧 カードゲーム ルフィ海賊団 出航！ゴーイング・メリー号　F626"))
    for expected in ["ワンピース", "カードゲーム", "海賊団", "ルフィ海賊団",
                     "ゴーイング・メリー", "F626"]:
        assert expected in terms, f"missing {expected} in {sorted(terms)}"


def test_boilerplate_is_stopped():
    assert tu.is_stopword("発送") and tu.is_stopword("神経質") and tu.is_stopword("綺麗")
    assert not tu.is_stopword("ハイパーバトル")


# --------------------------------------------------------------------------
# scoring
# --------------------------------------------------------------------------

def _learned():
    return learn(EXAMPLES, negatives=NEGATIVES, counts=COUNTS, name="op-ce2")


def test_all_four_real_listings_match():
    _alert, rules, _q, _r = _learned()
    for ex in EXAMPLES:
        v = evaluate(rules, ex.name, ex.description)
        assert v.matched, f"missed {ex.item_id}: score {v.score} < {rules.min_score}"


def test_negatives_do_not_match():
    _alert, rules, _q, _r = _learned()
    for ng in NEGATIVES:
        v = evaluate(rules, ng.name, ng.description)
        assert not v.matched, f"false positive on {ng.item_id}: {v.score}"


def test_unseen_phrasings_still_match():
    _alert, rules, _q, _r = _learned()
    unseen = [
        ("ワンピース ハイパーバトル C-E2", ""),
        ("ワンピース カードダス ルフィ海賊団 出航 ゴーイングメリー号", "旧カードゲーム"),
        ("ワンピース ハイパーバトル ＣＥ２ ルフィ海賊団", ""),
        ("ワンピース 旧カード 大会限定プロモ", "C-E2 ルフィ海賊団 出航！ゴーイング・メリー号"),
    ]
    for title, desc in unseen:
        assert evaluate(rules, title, desc).matched, f"missed unseen phrasing: {title}"


def test_wrong_card_in_same_series_is_not_a_match():
    _alert, rules, _q, _r = _learned()
    v = evaluate(rules, "ワンピース ハイパーバトル カードダス ゾロ", "ゾロのカードです")
    assert not v.matched, f"wrong card scored {v.score}"


def test_reproductions_are_excluded_by_default():
    _alert, rules, _q, _r = _learned()
    v = evaluate(rules, "ワンピース ルフィ海賊団 ゴーイング・メリー号 C-E2 複製", "")
    assert v.status == "reject" and v.excluded_by


def test_title_hits_outweigh_description_hits():
    rules = MatchRules(require=[], signals={"ハイパーバトル": 4.0}, min_score=1.0)
    in_title = evaluate(rules, "ハイパーバトル", "")
    in_desc = evaluate(rules, "なにか", "ハイパーバトル")
    assert in_title.score > in_desc.score


def test_needs_description_flag():
    _alert, rules, _q, _r = _learned()
    # Title alone is not enough here; the engine should ask for the body.
    v = evaluate(rules, "ワンピース 旧 カードゲーム 出品", None)
    assert v.needs_description


# --------------------------------------------------------------------------
# learning
# --------------------------------------------------------------------------

def test_idf_ranks_rare_terms_above_common_ones():
    assert idf_factor(45) > idf_factor(900) > idf_factor(480000)


def test_specific_terms_outweigh_family_terms():
    _alert, rules, _q, _r = _learned()
    assert rules.signals["C-E2"] > rules.signals["ワンピース"] * 5
    assert rules.signals["ゴーイング・メリー号"] > rules.signals["カードダス"]


def test_every_example_is_reachable_by_some_query():
    _alert, _rules, queries, _r = _learned()
    for i, ex in enumerate(EXAMPLES, 1):
        reachable = any(
            all(tu.contains(tok, ex.text_t) for tok in q["q"].split()) for q in queries
        )
        assert reachable, f"example {i} unreachable by any query"


def test_queries_are_not_self_redundant():
    _alert, _rules, queries, _r = _learned()
    for q in queries:
        toks = q["q"].split()
        tights = [tu.tight(t) for t in toks]
        for i, a in enumerate(tights):
            for j, b in enumerate(tights):
                assert i == j or a not in b, f"redundant query terms: {q['q']}"


def test_learner_does_not_require_the_product_code():
    # Requiring "C-E2" would silently miss every seller who omits the code.
    _alert, rules, _q, _r = _learned()
    for group in rules.require:
        assert not all(tu.tight(t) == tu.tight("C-E2") for t in group)


def test_learned_excludes_are_conservative():
    _alert, rules, _q, _r = _learned()
    from mlert.rules import DEFAULT_EXCLUDES
    learned = [e for e in rules.exclude if e not in DEFAULT_EXCLUDES]
    assert len(learned) <= 4
    for e in learned:
        assert not any(ex.has(e) for ex in EXAMPLES), f"exclude {e} kills a real example"


# --------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------

def test_relist_fingerprint_is_stable_across_ids():
    a = State.fingerprint("ワンピース C-E2 ルフィ海賊団", 98000, "seller1")
    b = State.fingerprint("ワンピース　C-E2　ルフィ海賊団", 98200, "seller1")
    c = State.fingerprint("ワンピース C-E2 ルフィ海賊団", 98000, "seller2")
    assert a == b, "same item relisted should fingerprint identically"
    assert a != c, "different sellers should not collide"


def test_state_roundtrip_and_v1_migration(tmp_path=None):
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "state.json"
        p.write_text(json.dumps({"old-alert": {"initialized": True, "seen_ids": ["m1", "m2"]}}),
                     "utf-8")
        st = State(p)
        assert st.seen("old-alert") == {"m1", "m2"}
        st.record_seen("new-alert", ["m9"])
        st.record_price("new-alert", 1000)
        st.save()
        st2 = State(p)
        assert st2.seen("new-alert") == {"m9"}
        assert not st2.is_first_run("new-alert")


# --------------------------------------------------------------------------

def _run_all():
    fns = [(n, f) for n, f in sorted(globals().items())
           if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in fns:
        try:
            fn()
            print(f"  PASS  {name}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {name}: {e}")
        except Exception as e:
            failed += 1
            print(f"  ERROR {name}: {type(e).__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
