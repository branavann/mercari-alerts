"""
The data files the web UI reads.

The UI is a static page - it cannot query Mercari. So every scheduled run
writes what it found into ui/data/*.json, which the page fetches. Keeping
this in the repo means the feed has history and needs no database.
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .mercari import item_url

FEED_MAX_ENTRIES = 400
FEED_MAX_AGE_DAYS = 45

# Mercari's item_condition_id values.
CONDITION_LABELS = {
    1: "New",
    2: "Like new",
    3: "Good",
    4: "Fair",
    5: "Worn",
    6: "Poor",
}


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse(ts):
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts))
    except ValueError:
        return None
    if dt.tzinfo is None:
        # mercapi returns naive datetimes in JST; treat them as UTC+9.
        dt = dt.replace(tzinfo=timezone(timedelta(hours=9)))
    return dt


def make_entry(alert, hit, status="match"):
    """Turn one scored listing into a feed record."""
    listed = _parse(hit.get("created"))
    detected = datetime.now(timezone.utc)
    lag = int((detected - listed).total_seconds()) if listed else None

    price = hit.get("price")
    target = alert.target_price
    target_hit = (
        isinstance(price, (int, float)) and target is not None and price <= target
    )

    return {
        "id": hit["id"],
        "url": item_url(hit["id"]),
        "marketplace": "mercari",
        "alert": alert.name,
        "alert_label": alert.label,
        "name": hit.get("name") or "",
        "price": price,
        "thumbnail": hit.get("thumbnail"),
        "seller_id": hit.get("seller_id"),
        "condition": CONDITION_LABELS.get(hit.get("condition_id")),
        "listed_at": listed.isoformat(timespec="seconds") if listed else None,
        "detected_at": detected.isoformat(timespec="seconds"),
        "lag_seconds": lag if (lag is not None and 0 <= lag < 60 * 60 * 24 * 400) else None,
        "score": hit.get("score"),
        "hits": (hit.get("hits") or [])[:8],
        "status": status,
        "deal": bool(hit.get("deal")),
        "relist": bool(hit.get("relist")),
        "target_hit": target_hit,
    }


class FeedWriter:
    """Append-only-ish JSON feed, pruned by age and size."""

    def __init__(self, path):
        self.path = Path(path)
        self.data = self._load()

    def _load(self):
        if self.path.exists():
            try:
                d = json.loads(self.path.read_text("utf-8"))
                if isinstance(d, dict) and isinstance(d.get("entries"), list):
                    return d
            except Exception:
                pass
        return {"generated_at": None, "entries": []}

    def add(self, entries):
        if not entries:
            return 0
        known = {e.get("id") for e in self.data["entries"]}
        fresh = [e for e in entries if e.get("id") not in known]
        self.data["entries"] = fresh + self.data["entries"]
        return len(fresh)

    def prune(self):
        cutoff = datetime.now(timezone.utc) - timedelta(days=FEED_MAX_AGE_DAYS)
        kept = []
        for e in self.data["entries"]:
            d = _parse(e.get("detected_at"))
            if d is None or d >= cutoff:
                kept.append(e)
        self.data["entries"] = kept[:FEED_MAX_ENTRIES]

    def save(self):
        self.prune()
        self.data["generated_at"] = now_iso()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=1), "utf-8"
        )

    def count_since(self, alert_name, hours=24):
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        n = 0
        for e in self.data["entries"]:
            if e.get("alert") != alert_name or e.get("status") != "match":
                continue
            d = _parse(e.get("detected_at"))
            if d and d >= cutoff:
                n += 1
        return n


def write_status(path, alerts, state, feed, extra=None):
    """Small summary file so the UI can show run health without the full feed."""
    path = Path(path)
    per_alert = {}
    for a in alerts:
        st = state.alert(a.name)
        per_alert[a.name] = {
            "label": a.label,
            "paused": a.paused,
            "queries": len(a.queries),
            "last_checked": st.get("last_checked"),
            "known_listings": len(st.get("seen_ids", [])),
            "median_price": state.median_price(a.name),
            "matches_24h": feed.count_since(a.name, 24),
            "matches_7d": feed.count_since(a.name, 24 * 7),
        }
    doc = {"last_run": now_iso(), "alerts": per_alert}
    doc.update(extra or {})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=1), "utf-8")
    return doc
