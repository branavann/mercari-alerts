"""
Learn a search strategy from example listings.

You hand it several past listings of the SAME item (sold ones are fine - the
page stays up), and it derives:

  1. a FAN-OUT of Mercari queries, including one built from each example's
     own title, so every phrasing style you've actually seen is covered
  2. weighted match signals, weighted by how *rare* each term is on Mercari
  3. a score threshold that all your examples clear with margin
  4. a coverage report proving each example is reachable

Why the fan-out matters. In the One Piece C-E2 sample, the four listings
share no single useful title keyword:

  A: ワンピース 旧 カードゲーム ルフィ海賊団 出航！ゴーイング・メリー号 F626
  B: カードダス ワンピース ハイパーバトル プロモカード1枚
  C: 【美品】ワンピース ハイパーバトル カードダス C-E2 ルフィ海賊団
  D: ワンピース 旧 カードゲーム ルフィ海賊団 出航！ゴーイング・メリー号 F650

"ハイパーバトル" is absent from A and D's titles. "ルフィ海賊団" is absent
from B's. The card code "C-E2" is in C's title only - the others bury it in
the description. One query cannot find all four. A set of queries can.

Why the IDF weighting matters. "ワンピース" matches hundreds of thousands of
listings; "ゴーイング・メリー号" matches a handful. Both appear in all four
examples, so pure frequency analysis rates them equally - and then a listing
for a *different* One Piece card scores just as high as the one you want.
Asking Mercari how common each term is fixes that, and costs a few API calls
once, at learn time.
"""

import math
from collections import defaultdict

from . import textutil as tu
from .rules import DEFAULT_EXCLUDES, MatchRules, evaluate

# Rough size of the live mercari.jp corpus, used as the IDF numerator. The
# exact value doesn't matter much - only the ratio between terms does.
IDF_CORPUS_SIZE = 1_000_000
IDF_MIN, IDF_MAX = 0.25, 5.0


class Example:
    """A listing used as training data."""

    def __init__(self, name, description="", item_id=None, price=None):
        self.name = name or ""
        self.description = description or ""
        self.item_id = item_id
        self.price = price
        self.title_t = tu.prep(self.name)
        self.text_t = tu.Prepped()
        d = tu.prep(self.description)
        self.text_t.t = self.title_t.t + "\x00" + d.t
        self.text_t.s = self.title_t.s + "\x00" + d.s

    def has(self, term):
        return tu.contains(term, self.text_t)

    def has_in_title(self, term):
        return tu.contains(term, self.title_t)


class Candidate:
    __slots__ = ("surface", "tight", "df_all", "df_title", "hint", "idf", "rank")

    def __init__(self, surface):
        self.surface = surface
        self.tight = tu.tight(surface)
        self.df_all = 0
        self.df_title = 0
        self.hint = tu.term_weight_hint(surface)
        self.idf = 1.0          # neutral until measured
        self.rank = 0.0


_COUNTER_RE = None


def _is_counter(surface):
    """True for pure quantity tokens like 1枚 / 100枚 / 3冊 / 2本."""
    global _COUNTER_RE
    if _COUNTER_RE is None:
        import re
        _COUNTER_RE = re.compile(r"^\d+(枚|個|点|本|冊|組|束|台|巻|セット)$")
    return bool(_COUNTER_RE.match(tu.nfkc(surface)))


def idf_factor(num_found):
    """Convert a live Mercari result count into a specificity multiplier."""
    if num_found is None:
        return 1.0
    v = math.log10(IDF_CORPUS_SIZE / max(int(num_found), 10))
    return max(IDF_MIN, min(IDF_MAX, v))


def _collect_candidates(examples):
    by_tight = {}
    for ex in examples:
        for surface in tu.extract_terms(ex.name) + tu.extract_terms(ex.description):
            t = tu.tight(surface)
            if not t:
                continue
            c = by_tight.get(t)
            if c is None:
                by_tight[t] = Candidate(surface)
            elif len(surface) < len(c.surface):
                c.surface = surface

    for c in by_tight.values():
        c.df_all = sum(1 for ex in examples if ex.has(c.surface))
        c.df_title = sum(1 for ex in examples if ex.has_in_title(c.surface))
    return by_tight


def _rank_candidates(cands, n):
    for c in cands:
        coverage = c.df_all / max(n, 1)
        title_bonus = 1.0 + 0.5 * (c.df_title / max(n, 1))
        c.rank = (coverage ** 2) * 10 * c.hint * title_bonus * c.idf
    return cands


def _drop_redundant(cands):
    """
    Collapse overlapping terms.
      * a shorter term with the SAME coverage as a longer one it sits inside
        is dropped   (ルフィ / 海賊団 -> ルフィ海賊団)
      * a longer term whose shorter part has STRICTLY BETTER coverage and is
        not itself generic is dropped  (ルフィ海賊団出航 -> ルフィ海賊団)
    """
    by_len = sorted(cands, key=lambda c: len(c.tight), reverse=True)
    kept = []
    for c in by_len:
        if any(c.tight != k.tight and c.tight in k.tight and c.df_all == k.df_all
               for k in kept):
            continue
        kept.append(c)

    final = []
    for c in kept:
        shadowed = any(
            o.tight != c.tight
            and o.tight in c.tight
            and o.df_all > c.df_all
            and not tu.is_generic(o.surface)
            for o in kept
        )
        if not shadowed:
            final.append(c)
    return final


def _pick_query_terms(ranked, k=2):
    """Take the top k terms, never two where one contains the other."""
    picked = []
    for c in ranked:
        if any(c.tight in p.tight or p.tight in c.tight for p in picked):
            continue
        picked.append(c)
        if len(picked) >= k:
            break
    return picked


# A term this rare on Mercari is worth querying on its own: maximum recall,
# and the local scoring pass handles precision anyway.
SOLO_QUERY_MAX_COUNT = 3000


def _build_queries(examples, cands_by_tight, max_queries=8, terms_per_query=2,
                   counts=None):
    def ranked_for(surfaces, prefer_title=True):
        out = []
        for s in surfaces:
            c = cands_by_tight.get(tu.tight(s))
            if not c or tu.is_stopword(s):
                continue
            score = c.rank
            if tu.is_generic(s):
                score *= 0.35
            if prefer_title and c.df_title == 0:
                score *= 0.7
            out.append((score, c))
        out.sort(key=lambda x: (-x[0], x[1].tight))
        return [c for _s, c in out]

    queries, seen = [], set()
    counts = counts or {}

    def push(cands, origin):
        if not cands:
            return
        q = " ".join(c.surface for c in cands)
        key = tu.tight(q)
        if key and key not in seen:
            seen.add(key)
            queries.append({"q": q, "from": origin})

    shared_all = _drop_redundant(
        [c for c in cands_by_tight.values() if c.df_all == len(examples)]
    )
    shared_all.sort(key=lambda c: -c.rank)

    # 0. solo queries for terms rare enough to search on their own
    for c in shared_all:
        cnt = counts.get(c.surface)
        if cnt is not None and cnt <= SOLO_QUERY_MAX_COUNT and not tu.is_generic(c.surface):
            push([c], f"rare term, {cnt:,} listings")

    # 1. one query per example, from that example's own title
    for i, ex in enumerate(examples, 1):
        r = ranked_for(tu.extract_terms(ex.name))
        push(_pick_query_terms(r, terms_per_query), f"title of example {i}")

    # 2. cross-example queries from the strongest universally-present terms
    shared = shared_all
    strong = [c for c in shared if not tu.is_generic(c.surface)][:4]
    for i in range(len(strong)):
        for j in range(i + 1, len(strong)):
            a, b = strong[i], strong[j]
            if a.tight in b.tight or b.tight in a.tight:
                continue
            push([a, b], "shared terms")
            if len(queries) >= max_queries:
                break
        if len(queries) >= max_queries:
            break

    # 3. broad anchor + product code (codes are the highest-value query term
    #    when a seller bothers to write one)
    codes = [c for c in shared
             if any(ch.isdigit() for ch in c.tight)
             and any(ch.isalpha() and ch.isascii() for ch in c.tight)]
    if codes and strong:
        broad = min(strong, key=lambda c: c.idf)  # the most common = best net
        if broad.tight != codes[0].tight:
            push([broad, codes[0]], "anchor + product code")

    return queries[:max_queries]


def learn(examples, negatives=None, max_queries=8, max_signals=12,
          label=None, name=None, counts=None):
    """
    counts: optional {surface_term: live_mercari_result_count}. Supplying it
    turns on IDF weighting, which is what separates "identifies the product
    family" terms from "identifies this exact item" terms.
    """
    negatives = negatives or []
    if not examples:
        raise ValueError("need at least one example listing")
    n = len(examples)
    counts = counts or {}

    cands_by_tight = _collect_candidates(examples)
    for c in cands_by_tight.values():
        if c.surface in counts:
            c.idf = idf_factor(counts[c.surface])
    _rank_candidates(list(cands_by_tight.values()), n)

    kept = _drop_redundant(list(cands_by_tight.values()))
    kept.sort(key=lambda c: -c.rank)

    # ---- excludes -------------------------------------------------------
    # An exclude is a blunt instrument - one bad term silently kills real
    # hits forever. So only learn a term that shows up in at least half the
    # negatives (i.e. it characterises what you're rejecting, rather than
    # being incidental to one listing).
    learned_excludes = []
    if negatives:
        neg_cands = _collect_candidates(negatives)
        need = max(2, math.ceil(len(negatives) / 2)) if len(negatives) > 1 else 1
        pool = [c for c in _drop_redundant(list(neg_cands.values()))
                if c.df_all >= need]
        pool.sort(key=lambda c: (-c.df_all, len(c.tight)))
        default_t = {tu.tight(x) for x in DEFAULT_EXCLUDES}
        for c in pool:
            if any(ex.has(c.surface) for ex in examples):
                continue
            if tu.is_stopword(c.surface) or tu.is_generic(c.surface):
                continue
            if len(c.tight) < 2 or c.tight in default_t:
                continue
            if _is_counter(c.surface):
                continue
            learned_excludes.append(c.surface)
            if len(learned_excludes) >= 4:
                break

    # Negatives that survive the built-in excludes are the ones that actually
    # threaten precision - use them to damp non-discriminating terms.
    base_rules = MatchRules(require=[], signals={}, exclude=list(DEFAULT_EXCLUDES))
    live_negs = [
        ng for ng in negatives
        if evaluate(base_rules, ng.name, ng.description).excluded_by is None
    ]

    def neg_penalty(surface):
        # Only damp terms present in EVERY surviving negative: those are
        # family words, not identity words.
        if not live_negs:
            return 1.0
        return 0.4 if all(ng.has(surface) for ng in live_negs) else 1.0

    # ---- signals --------------------------------------------------------
    signals = {}
    for c in kept:
        if tu.is_generic(c.surface) or tu.is_stopword(c.surface):
            continue
        if len(c.tight) < 2 or _is_counter(c.surface):
            continue
        # Require either universal coverage, or decent coverage backed by at
        # least one appearance in a TITLE (kills shared seller boilerplate).
        if not (c.df_all == n or (c.df_title >= 1 and c.df_all >= math.ceil(n / 2))):
            continue
        base = 3.0 if c.df_all == n else 2.0 if c.df_all >= n * 0.75 else 1.0
        w = base * c.idf * neg_penalty(c.surface)
        signals[c.surface] = round(max(0.2, min(25.0, w)), 2)
        if len(signals) >= max_signals:
            break

    if not signals:  # degenerate input - fall back to the top terms verbatim
        for c in kept[:5]:
            signals[c.surface] = 1.0

    # ---- require --------------------------------------------------------
    # ONE group, OR'd: at least one core identity term must be present. This
    # is deliberately loose - a listing that omits the product code should
    # still be catchable. Precision comes from the score threshold.
    universal = [c for c in kept
                 if c.df_all == n
                 and not tu.is_generic(c.surface)
                 and not tu.is_stopword(c.surface)]
    universal.sort(key=lambda c: (-c.df_title, -c.rank))
    anchor_group = _pick_query_terms(universal, 3)
    require = [[c.surface for c in anchor_group]] if anchor_group else []

    rules = MatchRules(require=require, signals=signals,
                       exclude=list(DEFAULT_EXCLUDES) + list(learned_excludes),
                       min_score=4.0)

    # ---- threshold ------------------------------------------------------
    # Anchor the threshold to the strongest signals, NOT to how chatty the
    # example descriptions happen to be. A terse listing that names the item
    # correctly must still clear the bar.
    pos = [evaluate(rules, ex.name, ex.description).score for ex in examples]
    neg_scores = [evaluate(rules, ng.name, ng.description).score for ng in live_negs]
    weakest_pos = min(pos) if pos else 8.0

    top = sorted(rules.signals.values(), reverse=True)
    k = 2 if counts else 3            # without IDF, demand more corroboration
    thr = 0.75 * sum(top[:k]) if top else 4.0
    if neg_scores:
        thr = max(thr, max(neg_scores) * 1.2 + 0.5)
    thr = min(thr, weakest_pos * 0.75)   # our own examples must pass easily
    rules.min_score = float(max(3.0, round(thr, 1)))

    queries = _build_queries(examples, cands_by_tight, max_queries=max_queries,
                             counts=counts)
    report = coverage_report(examples, live_negs, negatives, queries, rules, counts,
                             cands_by_tight)

    alert = {
        "name": name or "unnamed-alert",
        "label": label or (examples[0].name[:60] if examples else ""),
        "queries": [q["q"] for q in queries],
        "match": {
            "require": rules.require,
            "signals": dict(rules.signals),
            "exclude": learned_excludes,
            "min_score": rules.min_score,
            "scope": "full",
        },
        "learned_from": [ex.item_id for ex in examples if ex.item_id],
    }
    return alert, rules, queries, report


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

def _query_hits(query, ex, field):
    text = ex.title_t if field == "title" else ex.text_t
    return all(tu.contains(tok, text) for tok in query.split())


def coverage_report(examples, live_negs, all_negs, queries, rules, counts, cands):
    lines = ["QUERY FAN-OUT"]
    for q in queries:
        lines.append(f"  - {q['q']}    ({q['from']})")

    lines += ["", "RETRIEVAL COVERAGE   T = query matches the title alone,"
                  "  d = only via description"]
    no_title, no_any = [], []
    for i, ex in enumerate(examples, 1):
        marks = []
        for q in queries:
            if _query_hits(q["q"], ex, "title"):
                marks.append("T")
            elif _query_hits(q["q"], ex, "full"):
                marks.append("d")
            else:
                marks.append(".")
        if "T" not in marks:
            no_title.append(i)
        if "T" not in marks and "d" not in marks:
            no_any.append(i)
        lines.append(f"  ex{i}  [{''.join(marks)}]  {ex.name[:56]}")

    lines.append("")
    if no_any:
        lines.append(f"  !! examples {no_any} are unreachable by every query - add one by hand")
    elif no_title:
        lines.append(f"  ~  examples {no_title} rely on Mercari indexing description text.")
        lines.append("     Usually fine, but add a title-derived query if you see misses.")
    else:
        lines.append("  OK  every example is reachable by title alone (the safe case).")

    if counts:
        lines += ["", "TERM SPECIFICITY  (live Mercari result counts)"]
        rows = [(t, counts[t]) for t in counts]
        rows.sort(key=lambda r: r[1])
        for term, cnt in rows[:14]:
            w = rules.signals.get(term)
            wtxt = f"weight {w}" if w is not None else "not used"
            lines.append(f"  {cnt:>9,} listings   {term:<22} {wtxt}")

    learned = [e for e in rules.exclude if e not in DEFAULT_EXCLUDES]
    if learned:
        lines += ["", "LEARNED EXCLUDES  (review these - a wrong one silently kills real hits)"]
        for e in learned:
            lines.append(f"  - {e}")

    lines += ["", f"SCORING   threshold = {rules.min_score}"]
    for i, ex in enumerate(examples, 1):
        v = evaluate(rules, ex.name, ex.description)
        lines.append(f"  ex{i}   score {v.score:>7}   {'PASS' if v.matched else 'FAIL <-- fix'}")
    for i, ng in enumerate(all_negs, 1):
        v = evaluate(rules, ng.name, ng.description)
        if v.excluded_by:
            verdict = f"rejected by exclude '{v.excluded_by}'"
        elif v.matched:
            verdict = "FALSE POSITIVE <-- tighten"
        elif v.status == "borderline":
            verdict = "borderline (would land in the 'possible' section)"
        else:
            verdict = "correctly rejected"
        lines.append(f"  neg{i}  score {v.score:>7}   {verdict}")

    return "\n".join(lines)


def to_yaml_block(alert):
    import yaml
    return yaml.safe_dump({"alerts": [alert]}, allow_unicode=True,
                          sort_keys=False, width=100)
