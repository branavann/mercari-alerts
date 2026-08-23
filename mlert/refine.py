"""
Turning "not this one" clicks in the preview into rule changes.

The browser cannot segment Japanese, so it cannot work out which words
separate the listings you rejected from the ones you kept. This module does
that part in Python and ships the result with every preview card:

    terms    surface forms, for display        ["ルフィ海賊団", "バカンス", ...]
    tterms   tight() form of each, index-aligned with `terms`
    tt       tight() form of the listing's whole text

Given those three, choosing a safe exclude word becomes plain substring
arithmetic that the page can do instantly, with no second round trip to
Actions: a term is safe to exclude when its tight form appears in the `tt` of
no listing you kept.

That test is deliberately the same one mlert.rules.evaluate() will apply
later, minus the short-code boundary guard. Skipping the guard makes the
check slightly *stricter* than real matching (it can call a term unsafe when
matching would have let it through), and erring toward "don't suggest this"
is the harmless direction - a suggestion that silently killed a listing you
wanted would not be.

The ranking and set arithmetic live in ui/refine.js so the page can run them
on every click; this module owns the linguistics.
"""

from . import textutil as tu

# Per card. Enough to describe a listing, small enough that 24 cards of them
# stay a reasonable JSON payload.
CARD_TERM_LIMIT = 40


def card_terms(title, description=None, limit=CARD_TERM_LIMIT):
    """
    Extract the filterable terms of one listing.

    Returns {"terms": [...], "tterms": [...], "tt": "..."} with terms and
    tterms index-aligned. The most identifying terms come first, so that
    truncating at `limit` drops boilerplate rather than product codes.
    """
    text = " ".join(x for x in (title or "", description or "") if x).strip()
    if not text:
        return {"terms": [], "tterms": [], "tt": ""}

    ranked, seen = [], set()
    for surface in tu.extract_terms(text):
        t = tu.tight(surface)
        if not t or t in seen:
            continue
        seen.add(t)
        ranked.append((tu.term_weight_hint(surface), surface, t))

    # Weight first, then longer terms; sort is stable so extraction order
    # breaks any remaining ties.
    ranked.sort(key=lambda r: (-r[0], -len(r[2])))
    ranked = ranked[:limit]

    return {
        "terms": [s for _w, s, _t in ranked],
        "tterms": [t for _w, _s, t in ranked],
        # Words like レア / セット / 希少 describe any collectible, not this
        # one. They are still offerable - "no bundles" is a real wish - but
        # the panel must not tick them for you.
        "generic": [t for _w, s, t in ranked if tu.is_generic(s)],
        "tt": tu.tight(text),
    }


def rule_tights(rules):
    """
    The alert's current rules in tight form, so the page can avoid suggesting
    a word it already uses (or one that would fight a hard requirement).
    """
    return {
        "require": [[tu.tight(t) for t in group] for group in rules.require],
        "signals": {tu.tight(t): w for t, w in rules.signals.items()},
        "exclude": [tu.tight(t) for t in rules.exclude],
    }
