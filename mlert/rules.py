"""
Alert configuration model and the scoring engine.

An alert is deliberately split into two halves:

  queries   - what we ASK MERCARI for. Optimised for *recall*: several
              differently-phrased searches, because (as the One Piece C-E2
              examples prove) no single query catches every way sellers
              title the same item.

  match     - how we JUDGE what came back. Optimised for *precision*:
              hard requirements, weighted signals, hard exclusions, and a
              score threshold. Runs locally over title + description, so it
              sees far more than Mercari's own search ranking does.

Because the judging is strict, the queries are free to be broad.
"""

from dataclasses import dataclass, field

from . import textutil as tu

# Applied to every alert unless `use_default_excludes: false`. These are the
# things nobody ever wants to be alerted about.
DEFAULT_EXCLUDES = [
    # reproductions / fakes
    "複製", "リプロ", "レプリカ", "コピー品", "偽物", "自作", "ダミー",
    # not-the-item / want-to-buy posts
    "空箱", "箱のみ", "ケースのみ", "説明書のみ", "取扱説明書のみ", "外箱のみ",
    "求む", "探しています", "譲ってください", "買取",
]

# Optional named bundles you can switch on per alert with `presets: [...]`.
PRESETS = {
    "no_repro": ["複製", "リプロ", "レプリカ", "コピー品", "偽物", "自作", "ダミー"],
    "no_junk": ["ジャンク", "訳あり", "訳有り", "難あり", "破損", "汚れ大", "水濡れ跡"],
    "no_bulk": ["まとめ売り", "まとめて", "大量", "セット売り", "詰め合わせ", "福袋"],
    "no_parts": ["空箱", "箱のみ", "ケースのみ", "説明書のみ", "外箱のみ", "付属品のみ"],
    "sealed_only": [],       # pairs with require_any below in docs
    "no_graded": ["PSA", "BGS", "ARS", "鑑定済"],
}


@dataclass
class MatchRules:
    # Every group here must produce at least one hit. Keep this SHORT -
    # usually just the franchise / product-family anchor.
    require: list = field(default_factory=list)          # list[list[str]]
    # Weighted evidence. {"term": weight}
    signals: dict = field(default_factory=dict)
    # Any hit here rejects the listing outright.
    exclude: list = field(default_factory=list)
    # Score needed to be treated as a confident match.
    min_score: float = 4.0
    # Listings scoring at least this go into the "possible" section of the
    # email instead of being dropped silently, so you can tune the rules.
    borderline_ratio: float = 0.55
    # Where to look. "title" is cheap; "full" fetches the description.
    scope: str = "full"                                   # "full" | "title"

    @classmethod
    def from_dict(cls, d, use_default_excludes=True, presets=None):
        d = d or {}
        require = [g if isinstance(g, list) else [g] for g in (d.get("require") or [])]

        raw_signals = d.get("signals") or {}
        if isinstance(raw_signals, list):
            signals = {s: 1.0 for s in raw_signals}
        else:
            signals = {k: float(v) for k, v in raw_signals.items()}

        exclude = list(d.get("exclude") or [])
        if use_default_excludes:
            exclude = list(DEFAULT_EXCLUDES) + exclude
        for name in presets or []:
            exclude += PRESETS.get(name, [])

        # de-dup excludes by tight form
        seen, ded = set(), []
        for e in exclude:
            t = tu.tight(e)
            if t and t not in seen:
                seen.add(t)
                ded.append(e)

        return cls(
            require=require,
            signals=signals,
            exclude=ded,
            min_score=float(d.get("min_score", 4.0)),
            borderline_ratio=float(d.get("borderline_ratio", 0.55)),
            scope=d.get("scope", "full"),
        )

    def to_dict(self):
        return {
            "require": self.require,
            "signals": self.signals,
            "exclude": self.exclude,
            "min_score": self.min_score,
            "scope": self.scope,
        }


@dataclass
class Verdict:
    status: str            # "match" | "borderline" | "reject"
    score: float
    hits: list             # signal terms that fired
    missing_required: list # required groups with no hit
    excluded_by: str = None
    needs_description: bool = False

    @property
    def matched(self):
        return self.status == "match"


# Title text is stronger evidence than description text - a term in the title
# is what the seller thinks the item IS.
TITLE_MULTIPLIER = 1.25


def evaluate(rules: MatchRules, title: str, description: str = None) -> Verdict:
    """
    Score one listing. Pass description=None when you only have the title
    (search results); the returned Verdict will set needs_description=True
    if fetching the full item could change the outcome.
    """
    title_t = tu.prep(title or "")
    desc_t = tu.prep(description or "")
    # NUL separator so no term can accidentally straddle title and description
    both_t = tu.Prepped()
    both_t.t = title_t.t + "\x00" + desc_t.t
    both_t.s = title_t.s + "\x00" + desc_t.s
    have_desc = description is not None

    # 1. Hard exclusions first (cheapest way to drop junk).
    for term in rules.exclude:
        if tu.contains(term, both_t):
            return Verdict("reject", 0.0, [], [], excluded_by=term)

    # 2. Hard requirements.
    missing = []
    for group in rules.require:
        if not tu.contains_any(group, both_t):
            missing.append(group)

    # 3. Weighted signals.
    score, hits = 0.0, []
    for term, weight in rules.signals.items():
        if tu.contains(term, title_t):
            score += weight * TITLE_MULTIPLIER
            hits.append(term)
        elif desc_t.t and tu.contains(term, desc_t):
            score += weight
            hits.append(term)

    if missing:
        # Could a description rescue it? Only if we haven't looked yet.
        return Verdict(
            "reject", score, hits, missing,
            needs_description=(not have_desc and rules.scope == "full"),
        )

    borderline_cut = rules.min_score * rules.borderline_ratio
    if score >= rules.min_score:
        status = "match"
    elif score >= borderline_cut:
        status = "borderline"
    else:
        status = "reject"

    needs_desc = (
        not have_desc
        and rules.scope == "full"
        and status != "match"          # a title-only match is already decided
    )
    return Verdict(status, round(score, 2), hits, [], needs_description=needs_desc)


def explain(rules: MatchRules, title: str, description: str = None) -> str:
    v = evaluate(rules, title, description)
    lines = [f"  title : {title}", f"  status: {v.status}  score={v.score} (need {rules.min_score})"]
    if v.excluded_by:
        lines.append(f"  killed by exclude term: {v.excluded_by}")
    if v.missing_required:
        lines.append(f"  missing required group(s): {v.missing_required}")
    if v.hits:
        lines.append(f"  signals hit: {', '.join(v.hits)}")
    else:
        lines.append("  signals hit: (none)")
    return "\n".join(lines)
