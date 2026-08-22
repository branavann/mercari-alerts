"""
Persistent state: what we've already seen, price history, and relist detection.

Stored as one JSON file that GitHub Actions commits back to the repo after
each run, so state survives between runs without a database.
"""

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import textutil as tu

SCHEMA_VERSION = 2
MAX_SEEN_PER_ALERT = 600
MAX_PRICE_HISTORY = 60
FINGERPRINT_TTL_DAYS = 45
MAX_FINGERPRINTS = 4000


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse(ts):
    try:
        return datetime.fromisoformat(ts)
    except Exception:
        return datetime.now(timezone.utc)


class State:
    def __init__(self, path):
        self.path = Path(path)
        self.data = self._load()

    def _load(self):
        if not self.path.exists():
            return {"version": SCHEMA_VERSION, "alerts": {}, "fingerprints": {}}
        raw = json.loads(self.path.read_text("utf-8"))
        if raw.get("version") == SCHEMA_VERSION:
            raw.setdefault("alerts", {})
            raw.setdefault("fingerprints", {})
            return raw
        # migrate the v1 flat format {alert_name: {initialized, seen_ids}}
        alerts = {}
        for k, v in raw.items():
            if isinstance(v, dict) and "seen_ids" in v:
                alerts[k] = {
                    "initialized": bool(v.get("initialized")),
                    "seen_ids": list(v.get("seen_ids", [])),
                    "prices": [],
                    "last_checked": None,
                }
        return {"version": SCHEMA_VERSION, "alerts": alerts, "fingerprints": {}}

    def save(self):
        self._prune()
        self.path.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=1, sort_keys=True), "utf-8"
        )

    # -- per-alert --------------------------------------------------------

    def alert(self, name):
        return self.data["alerts"].setdefault(
            name, {"initialized": False, "seen_ids": [], "prices": [], "last_checked": None}
        )

    def is_first_run(self, name):
        return not self.alert(name).get("initialized", False)

    def seen(self, name):
        return set(self.alert(name).get("seen_ids", []))

    def record_seen(self, name, newest_first_ids):
        a = self.alert(name)
        merged = list(dict.fromkeys(list(newest_first_ids) + a.get("seen_ids", [])))
        a["seen_ids"] = merged[:MAX_SEEN_PER_ALERT]
        a["initialized"] = True
        a["last_checked"] = now_iso()

    def due(self, name, min_interval_minutes):
        if not min_interval_minutes:
            return True
        last = self.alert(name).get("last_checked")
        if not last:
            return True
        return datetime.now(timezone.utc) - _parse(last) >= timedelta(
            minutes=float(min_interval_minutes)
        )

    # -- price stats ------------------------------------------------------

    def record_price(self, name, price):
        if not isinstance(price, (int, float)) or price <= 0:
            return
        a = self.alert(name)
        a.setdefault("prices", []).insert(0, int(price))
        a["prices"] = a["prices"][:MAX_PRICE_HISTORY]

    def median_price(self, name):
        prices = sorted(self.alert(name).get("prices", []))
        if len(prices) < 4:
            return None
        mid = len(prices) // 2
        if len(prices) % 2:
            return prices[mid]
        return (prices[mid - 1] + prices[mid]) / 2

    # -- relist detection -------------------------------------------------

    @staticmethod
    def fingerprint(title, price, seller_id=None):
        """
        Sellers routinely delete and re-post the same item to bump it up the
        feed, which gives it a brand-new item id. Fingerprinting on the
        normalised title + price bucket + seller catches that.
        """
        bucket = int(price // 500) if isinstance(price, (int, float)) and price else 0
        raw = f"{tu.tight(title)}|{bucket}|{seller_id or ''}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]

    def seen_fingerprint(self, fp):
        return fp in self.data["fingerprints"]

    def record_fingerprint(self, fp, item_id):
        self.data["fingerprints"][fp] = {"id": item_id, "t": now_iso()}

    # -- housekeeping -----------------------------------------------------

    def _prune(self):
        cutoff = datetime.now(timezone.utc) - timedelta(days=FINGERPRINT_TTL_DAYS)
        fps = self.data.get("fingerprints", {})
        fresh = {k: v for k, v in fps.items() if _parse(v.get("t", "")) >= cutoff}
        if len(fresh) > MAX_FINGERPRINTS:
            newest = sorted(fresh.items(), key=lambda kv: kv[1].get("t", ""), reverse=True)
            fresh = dict(newest[:MAX_FINGERPRINTS])
        self.data["fingerprints"] = fresh
