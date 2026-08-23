"""
Tests for the reject-and-refine feedback loop (Python half).

ui/refine.js decides which words to propose, using only substring tests over
the normalised fields this module produces. That shortcut is only legitimate
if two contracts hold, and both are asserted here:

  1. every `tterm` is a substring of `tt`
  2. a term that is NOT a substring of a listing's `tt` can never match that
     listing under mlert.rules - i.e. "safe to exclude" really is safe

Test the JS half with:  node tests/test_refine_js.mjs

    python tests/test_refine.py
"""

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mlert import textutil as tu                       # noqa: E402
from mlert.config import Alert, load_alerts            # noqa: E402
from mlert.refine import card_terms, rule_tights       # noqa: E402
from mlert.rules import MatchRules, evaluate           # noqa: E402

# Real titles from a live preview: the same Hyper Battle card family, but
# four different cards. Only the first is the one being hunted.
WANTED = "【超希少】ONE PIECE ハイパーバトル ルフィ海賊団「冒険を求めて」C-E2"
OTHERS = [
    "ワンピース カードダス ハイパーバトル C364 ルフィ海賊団　バカンス",
    "ワンピース　ハイパーバトル　カードダス　ルフィ海賊団　花火見物　旧　初期レア",
    "ワンピース　ハイパーバトル　カードダス　ルフィ海賊団　集結！　初期　旧　レア",
    "ワンピースカードダス ハイパーバトル ７枚セット プロモ ルフィ海賊団",
]


# --------------------------------------------------------------------------
# the two contracts refine.js depends on
# --------------------------------------------------------------------------

def test_every_term_is_a_substring_of_the_tight_text():
    for title in [WANTED] + OTHERS:
        c = card_terms(title, "説明文です ゴーイング・メリー号")
        for surface, t in zip(c["terms"], c["tterms"]):
            assert t == tu.tight(surface), f"{surface} -> {t}"
            assert t in c["tt"], f"{t!r} not found in tt for {title!r}"


def test_absent_from_tight_text_means_it_cannot_match():
    """
    The safety property. refine.js only proposes excluding a word when it is
    absent from every kept listing's `tt`. If that could still match under the
    real engine, refining would silently delete listings you wanted.
    """
    kept = card_terms(WANTED)
    vocabulary = set()
    for title in OTHERS:
        c = card_terms(title)
        vocabulary.update(zip(c["terms"], c["tterms"]))

    checked = 0
    for surface, t in vocabulary:
        if t in kept["tt"]:
            continue                       # refine.js would not offer this one
        rules = MatchRules(require=[], signals={}, exclude=[surface], min_score=1)
        v = evaluate(rules, WANTED, None)
        assert v.excluded_by is None, (
            f"{surface!r} looked safe but would have killed the wanted listing")
        checked += 1
    assert checked > 3, f"only {checked} terms exercised the property"


def test_the_family_terms_are_not_separable():
    """Words common to wanted and unwanted must appear in both vocabularies."""
    kept = card_terms(WANTED)
    for title in OTHERS:
        c = card_terms(title)
        assert any(tu.tight("ハイパーバトル") == t for t in c["tterms"])
        assert tu.tight("ハイパーバトル") in kept["tt"]


def test_the_distinguishing_terms_are_actually_found():
    """バカンス / 花火見物 are what separate these cards; they must survive."""
    vac = card_terms(OTHERS[0])
    assert any("ばかんす" == t for t in vac["tterms"]), vac["tterms"]
    han = card_terms(OTHERS[1])
    # kanji are not folded, only katakana -> hiragana
    assert any(t == "花火見物" for t in han["tterms"]), han["tterms"]
    # ...and none of them appear in the card we want
    kept = card_terms(WANTED)
    assert "ばかんす" not in kept["tt"]


# --------------------------------------------------------------------------
# extraction behaviour
# --------------------------------------------------------------------------

def test_empty_input_is_handled():
    c = card_terms(None, None)
    assert c == {"terms": [], "tterms": [], "tt": ""}


def test_description_is_included():
    c = card_terms("ワンピース", "C-E2 ゴーイング・メリー号")
    assert "ce2" in c["tt"]
    assert any(t == "ce2" for t in c["tterms"]), c["tterms"]


def test_boilerplate_is_not_offered_as_a_filter():
    c = card_terms("ワンピース C-E2", "即購入OK 送料無料 神経質な方はご遠慮ください")
    for junk in ("即購入", "送料", "神経質"):
        assert not any(tu.tight(junk) == t for t in c["tterms"]), \
            f"{junk} should be stopped, got {c['terms']}"


def test_terms_are_capped_and_ranked():
    long_title = " ".join(f"タイトル{i}語 ABC{i}" for i in range(60))
    c = card_terms(long_title, limit=10)
    assert len(c["terms"]) == 10 == len(c["tterms"])
    # A code-shaped term outranks a plain one, so the cap keeps the useful end.
    c2 = card_terms("ワンピース カード C-E2")
    assert c2["terms"][0] != "カード", c2["terms"]


def test_rule_tights_normalises_every_side():
    rules = MatchRules(require=[["ワンピース", "ONE PIECE"]],
                       signals={"C-E2": 13.0}, exclude=["複製"], min_score=5)
    rt = rule_tights(rules)
    assert rt["require"] == [[tu.tight("ワンピース"), tu.tight("ONE PIECE")]]
    assert rt["signals"] == {"ce2": 13.0}
    assert rt["exclude"] == [tu.tight("複製")]


# --------------------------------------------------------------------------
# rejected ids persist and are honoured
# --------------------------------------------------------------------------

def test_alert_carries_rejected_ids():
    a = Alert({"name": "x", "queries": ["q"], "rejected_ids": ["m1", "m2", "m2"]})
    assert a.rejected_ids == {"m1", "m2"}
    assert Alert({"name": "y", "queries": ["q"]}).rejected_ids == set()


def test_rejected_ids_survive_a_ui_round_trip():
    ui_alert = {
        "name": "op-ce2", "label": "C-E2", "queries": ["C-E2"],
        "match": {"require": [["ワンピース"]], "signals": {"C-E2": 13.0},
                  "exclude": ["バカンス"], "min_score": 10, "scope": "full"},
        "rejected_ids": ["m64572484257", "m83971401198"],
    }
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        (td / "alerts.json").write_text(
            json.dumps({"alerts": [ui_alert]}, ensure_ascii=False), "utf-8")
        (a,) = load_alerts(td / "alerts.yaml", td / "alerts.json")
        assert a.rejected_ids == {"m64572484257", "m83971401198"}
        # the exclude the refiner added must actually bite
        assert evaluate(a.rules, OTHERS[0], "").status == "reject"
        # ...without touching the card we want
        assert evaluate(a.rules, WANTED, "").excluded_by is None


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
