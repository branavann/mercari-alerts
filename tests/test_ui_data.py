"""
Tests for the pieces the web UI depends on: feed records, status output,
and loading alerts from alerts.json + alerts.yaml together.

    python tests/test_ui_data.py
"""

import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mlert.config import Alert, load_alerts                      # noqa: E402
from mlert.feed import (CONDITION_LABELS, FeedWriter, make_entry,  # noqa: E402
                        write_status)
from mlert.state import State                                    # noqa: E402

ALERT = Alert({
    "name": "op-ce2", "label": "One Piece C-E2",
    "queries": ["C-E2", "ゴーイング・メリー号"],
    "target_price": 100000,
    "match": {"require": [["ワンピース"]], "signals": {"C-E2": 13.0}, "min_score": 10},
})


def hit(**kw):
    base = {"id": "m1", "name": "ワンピース C-E2", "price": 98000,
            "thumbnail": "https://x/i.jpg", "seller_id": "s1", "condition_id": 3,
            "score": 34.2, "hits": ["C-E2"]}
    base.update(kw)
    return base


# --------------------------------------------------------------------------

def test_entry_has_everything_the_ui_renders():
    e = make_entry(ALERT, hit(), "match")
    for k in ("id", "url", "marketplace", "alert", "alert_label", "name", "price",
              "thumbnail", "condition", "detected_at", "score", "hits", "status",
              "deal", "relist", "target_hit"):
        assert k in e, f"feed entry missing {k}"
    assert e["url"] == "https://jp.mercari.com/item/m1"
    assert e["condition"] == CONDITION_LABELS[3]


def test_target_price_flag():
    assert make_entry(ALERT, hit(price=90000))["target_hit"] is True
    assert make_entry(ALERT, hit(price=180000))["target_hit"] is False
    # no price at all must not crash or claim a hit
    assert make_entry(ALERT, hit(price=None))["target_hit"] is False


def test_lag_from_naive_jst_timestamp():
    # mercapi hands back naive datetimes in JST; a naive value read as UTC
    # would produce a 9-hour lag on a listing posted seconds ago.
    jst = timezone(timedelta(hours=9))
    listed = datetime.now(jst).replace(tzinfo=None) - timedelta(seconds=30)
    e = make_entry(ALERT, hit(created=listed.isoformat()))
    assert e["lag_seconds"] is not None
    assert 0 <= e["lag_seconds"] < 300, f"lag looks wrong: {e['lag_seconds']}s"


def test_absurd_lag_is_dropped_not_displayed():
    e = make_entry(ALERT, hit(created="1999-01-01T00:00:00"))
    assert e["lag_seconds"] is None


def test_feed_dedupes_and_prunes():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "feed.json"
        f = FeedWriter(p)
        assert f.add([make_entry(ALERT, hit(id="a")), make_entry(ALERT, hit(id="b"))]) == 2
        assert f.add([make_entry(ALERT, hit(id="b"))]) == 0, "duplicate id was re-added"
        f.data["entries"].append({"id": "old", "alert": "op-ce2", "status": "match",
                                  "detected_at": "2001-01-01T00:00:00+00:00"})
        f.save()
        reloaded = FeedWriter(p)
        ids = {e["id"] for e in reloaded.data["entries"]}
        assert ids == {"a", "b"}, f"stale entry not pruned: {ids}"
        assert reloaded.data["generated_at"]


def test_feed_counts_recent_matches_only():
    with tempfile.TemporaryDirectory() as td:
        f = FeedWriter(Path(td) / "feed.json")
        f.add([make_entry(ALERT, hit(id="new1"), "match"),
               make_entry(ALERT, hit(id="new2"), "borderline")])
        f.data["entries"].append({"id": "old", "alert": "op-ce2", "status": "match",
                                  "detected_at": (datetime.now(timezone.utc)
                                                  - timedelta(days=3)).isoformat()})
        assert f.count_since("op-ce2", 24) == 1      # borderline and old excluded
        assert f.count_since("op-ce2", 24 * 7) == 2
        assert f.count_since("other", 24) == 0


def test_status_file_shape():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        st = State(td / "state.json")
        st.record_seen("op-ce2", ["m1", "m2"])
        f = FeedWriter(td / "feed.json")
        f.add([make_entry(ALERT, hit(id="z"))])
        doc = write_status(td / "status.json", [ALERT], st, f)
        assert doc["last_run"]
        a = doc["alerts"]["op-ce2"]
        for k in ("label", "paused", "queries", "last_checked", "matches_24h", "median_price"):
            assert k in a, f"status missing {k}"
        assert json.loads((td / "status.json").read_text("utf-8"))["alerts"]["op-ce2"]["queries"] == 2


# --------------------------------------------------------------------------

def _write(p, obj, as_yaml=False):
    import yaml
    p.write_text(yaml.safe_dump(obj, allow_unicode=True) if as_yaml
                 else json.dumps(obj, ensure_ascii=False), "utf-8")


def test_loads_json_and_yaml_together():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        _write(td / "alerts.json", {"alerts": [{"name": "from-json", "queries": ["a"]}]})
        _write(td / "alerts.yaml", {"alerts": [{"name": "from-yaml", "queries": ["b"]}]}, True)
        got = {a.name: a.source for a in load_alerts(td / "alerts.yaml", td / "alerts.json")}
        assert got == {"from-json": "json", "from-yaml": "yaml"}


def test_duplicate_name_across_files_is_rejected():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        _write(td / "alerts.json", {"alerts": [{"name": "dup", "queries": ["a"]}]})
        _write(td / "alerts.yaml", {"alerts": [{"name": "dup", "queries": ["b"]}]}, True)
        try:
            load_alerts(td / "alerts.yaml", td / "alerts.json")
        except ValueError as e:
            assert "dup" in str(e)
        else:
            raise AssertionError("duplicate alert name was accepted")


def test_missing_files_are_fine():
    with tempfile.TemporaryDirectory() as td:
        assert load_alerts(Path(td) / "nope.yaml", Path(td) / "nope.json") == []


def test_ui_written_alert_round_trips():
    """An alert exactly as ui/index.html serialises it must load and score."""
    ui_alert = {
        "name": "ws-op", "label": "WS One Piece",
        "queries": ["ワンダースワン ONE PIECE"],
        "match": {"require": [["ワンダースワン"]],
                  "signals": {"ワンダースワン": 2, "ONE": 2, "PIECE": 2},
                  "exclude": ["ジャンク"], "min_score": 5.4, "scope": "full"},
        "presets": ["no_bulk"], "notify_email": True, "suppress_relists": False,
        "per_query_limit": 30, "price_max": 50000, "target_price": 8000,
        "created_at": "2026-08-23T00:00:00.000Z",
    }
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        _write(td / "alerts.json", {"alerts": [ui_alert]})
        (a,) = load_alerts(td / "alerts.yaml", td / "alerts.json")
        assert a.target_price == 8000 and a.price_max == 50000
        assert a.notify_email is True

        from mlert.rules import evaluate
        good = evaluate(a.rules, "ワンダースワン ONE PIECE めざせ海賊王", "")
        assert good.matched, f"UI-built rules failed their own query: {good.score}"
        junk = evaluate(a.rules, "ワンダースワン ONE PIECE ジャンク", "")
        assert junk.status == "reject", "preset/exclude did not apply"
        # no_bulk preset came through
        assert any("まとめ" in e for e in a.rules.exclude)


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
