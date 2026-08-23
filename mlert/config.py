"""
Loading and validating alert definitions.

Alerts can live in two files:

  alerts.json  - owned by the web UI. Machine-written, safe to rewrite.
  alerts.yaml  - hand-editable, with comments. The UI never touches it.

Both are loaded and merged. Names must be unique across the two.
"""

import json
from pathlib import Path

import yaml

from .rules import MatchRules


class Alert:
    def __init__(self, d, source="yaml"):
        self.raw = d
        self.source = source
        self.name = d["name"]
        self.label = d.get("label") or self.name
        self.paused = bool(d.get("paused", False))
        self.created_at = d.get("created_at")

        queries = d.get("queries")
        if not queries:
            kw = d.get("keyword")            # v1 single-keyword format
            queries = [kw] if kw else []
        self.queries = [q for q in queries if q and str(q).strip()]

        self.price_min = d.get("price_min")
        self.price_max = d.get("price_max")
        self.target_price = d.get("target_price")
        self.categories = d.get("categories") or []
        self.mercari_exclude = d.get("mercari_exclude")   # server-side exclude string
        self.min_interval_minutes = d.get("min_interval_minutes")
        self.per_query_limit = int(d.get("per_query_limit", 30))
        self.suppress_relists = bool(d.get("suppress_relists", False))
        self.notify_email = bool(d.get("notify_email", True))
        self.notes = d.get("notes") or ""
        # Listings dismissed by hand in the panel's preview. Kept as a list of
        # item ids so a rejection sticks even when the rules still match it -
        # some listings are simply wrong in a way no keyword describes.
        self.rejected_ids = set(str(x) for x in (d.get("rejected_ids") or []) if x)

        self.rules = MatchRules.from_dict(
            d.get("match"),
            use_default_excludes=bool(d.get("use_default_excludes", True)),
            presets=d.get("presets"),
        )
        # A keyword-only alert with no match rules: require the keyword itself,
        # so the simplest possible config still behaves sensibly.
        if not self.rules.require and not self.rules.signals and self.queries:
            self.rules.require = [[self.queries[0]]]
            self.rules.signals = {self.queries[0]: 5.0}
            self.rules.min_score = 3.0
            self.rules.scope = (d.get("match") or {}).get("scope", "title")

    def __repr__(self):
        return f"<Alert {self.name} ({len(self.queries)} queries)>"


def _validate(raw, seen, source):
    if not isinstance(raw, dict) or "name" not in raw:
        raise ValueError(f"every alert needs a 'name': {raw!r}")
    if not (raw.get("queries") or raw.get("keyword")):
        raise ValueError(f"alert '{raw['name']}' has no queries")
    if raw["name"] in seen:
        raise ValueError(
            f"duplicate alert name '{raw['name']}' "
            f"(defined in both alerts.json and alerts.yaml?)"
        )
    seen.add(raw["name"])


def load_alerts(yaml_path, json_path=None):
    """Load alerts from alerts.json and/or alerts.yaml."""
    alerts, seen = [], set()

    json_path = Path(json_path) if json_path else None
    if json_path and json_path.exists():
        data = json.loads(json_path.read_text("utf-8")) or {}
        for raw in data.get("alerts") or []:
            _validate(raw, seen, "alerts.json")
            alerts.append(Alert(raw, source="json"))

    yaml_path = Path(yaml_path)
    if yaml_path.exists():
        data = yaml.safe_load(yaml_path.read_text("utf-8")) or {}
        for raw in data.get("alerts") or []:
            _validate(raw, seen, "alerts.yaml")
            alerts.append(Alert(raw, source="yaml"))

    return alerts


def save_alerts_json(path, alert_dicts):
    """Rewrite alerts.json. Used by the UI task runner, not the checker."""
    path = Path(path)
    path.write_text(
        json.dumps({"alerts": alert_dicts}, ensure_ascii=False, indent=2), "utf-8"
    )


def append_alert(path, alert_dict):
    """Append a learned alert to alerts.yaml, preserving what's already there."""
    path = Path(path)
    data = yaml.safe_load(path.read_text("utf-8")) if path.exists() else None
    data = data or {}
    data.setdefault("alerts", [])
    data["alerts"] = [a for a in data["alerts"] if a.get("name") != alert_dict["name"]]
    data["alerts"].append(alert_dict)
    path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=100), "utf-8"
    )
