/*
 * Turn rejected preview listings into proposed rule changes.
 *
 * Everything here is pure set arithmetic over the pre-normalised fields that
 * tools/ui_task.py attaches to each preview card (see mlert/refine.py):
 *
 *     tt      the whole listing, tight-normalised
 *     terms   candidate surface forms, best first
 *     tterms  tight form of each, index-aligned with terms
 *
 * Because Python already did the Japanese segmentation and normalisation,
 * this file needs no knowledge of Japanese at all - just indexOf. That is
 * what lets the page respond to a click instantly instead of dispatching
 * another minute-long workflow.
 *
 * Loaded both by ui/index.html and by tests/test_refine_js.mjs.
 */
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.MREFINE = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  var MAX_EXCLUDES = 12;
  var MAX_REQUIRES = 8;

  function has(card, tterm) {
    return !!card && !!card.tt && card.tt.indexOf(tterm) !== -1;
  }

  /* Distinct {tterm -> surface} across a set of cards, best surface kept. */
  function vocab(cards) {
    var out = Object.create(null);
    cards.forEach(function (c) {
      (c.tterms || []).forEach(function (t, i) {
        if (t && !(t in out)) out[t] = (c.terms || [])[i] || t;
      });
    });
    return out;
  }

  /*
   * Drop a term when a shorter one in the same list already covers it.
   * "海賊団バカンス" is redundant next to "バカンス", and the shorter word is
   * the better exclude because it also catches next week's differently-worded
   * relist. Only applied when the shorter term is at least as well-evidenced.
   */
  function dropRedundant(rows, preferShorter) {
    return rows.filter(function (a) {
      return !rows.some(function (b) {
        if (a === b) return false;
        var aInB = b.tterm.indexOf(a.tterm) !== -1;
        var bInA = a.tterm.indexOf(b.tterm) !== -1;
        if (preferShorter) return bInA && b.count >= a.count;
        return aInB && b.count >= a.count;
      });
    });
  }

  /**
   * @param groups   {confirmed:[card], rejected:[card], unmarked:[card]}
   *                 confirmed = "yes, this is the item"; rejected = "no";
   *                 unmarked  = not looked at, and therefore not evidence.
   * @param current  {require:[[t]], signals:{t:w}, exclude:[t]} in tight form
   * @param opts     {minScore: number}
   *
   * Marking something correct is far stronger evidence than not rejecting
   * it, so once anything is confirmed the confirmed set becomes the thing
   * being protected, and unmarked listings stop counting as "keep". They are
   * still reported as collateral, because dropping one is a real cost.
   */
  function suggest(groups, current, opts) {
    groups = groups || {};
    var confirmed = groups.confirmed || [];
    var rejected = groups.rejected || [];
    var unmarked = groups.unmarked || [];
    current = current || {};
    opts = opts || {};

    // With nothing confirmed, "not rejected" is the best evidence available.
    var haveYes = confirmed.length > 0;
    var kept = haveYes ? confirmed : unmarked;

    var res = {
      excludes: [], requires: [], threshold: null, warnings: [],
      confirmedCount: confirmed.length, rejectedCount: rejected.length,
      unmarkedCount: unmarked.length, keptCount: kept.length,
    };
    if (!rejected.length) return res;

    var alreadyExcluded = Object.create(null);
    (current.exclude || []).forEach(function (t) { alreadyExcluded[t] = true; });
    var isRequired = Object.create(null);
    (current.require || []).forEach(function (g) {
      (g || []).forEach(function (t) { isRequired[t] = true; });
    });

    var isGeneric = Object.create(null);
    confirmed.concat(rejected, unmarked).forEach(function (c) {
      (c.generic || []).forEach(function (t) { isGeneric[t] = true; });
    });

    /*
     * A term made up ENTIRELY of words the alert already uses.
     *
     * "ワンピースカードダス" can be missing from every confirmed listing purely
     * because those sellers typed "ワンピース カードダス" with a space.
     * Excluding it would look safe and then bin the next genuine listing that
     * runs the words together, so it must never be offered.
     *
     * Merely *containing* one of those words is not enough to disqualify a
     * term, though: 冒険の夜明け contains 冒険, but 夜明け is the name of a
     * different card and excluding it is exactly the point. So strip the
     * alert's own vocabulary out and see whether anything of substance is
     * left over.
     */
    var ownWords = Object.keys(isRequired)
      .concat(Object.keys(current.signals || {}))
      .filter(Boolean)
      .sort(function (a, b) { return b.length - a.length; });   // longest first

    function isOwnCompound(t) {
      var rest = t;
      ownWords.forEach(function (w) { rest = rest.split(w).join(""); });
      // Whatever survives, minus grammar, has to be a real word of its own.
      return rest.replace(/[ぁ-ゖー\s]/g, "").length < 1 &&
             rest.replace(/\s/g, "").length < 2;
    }

    /* ---- exclude candidates: in the rejected, in none of the kept ---- */
    var rv = vocab(rejected);
    var exRows = [];
    Object.keys(rv).forEach(function (t) {
      if (alreadyExcluded[t] || isRequired[t] || isOwnCompound(t)) return;
      // Never propose excluding a word that a kept listing also uses - that
      // would throw away the very thing being previewed.
      if (kept.some(function (c) { return has(c, t); })) return;
      var n = rejected.filter(function (c) { return has(c, t); }).length;
      if (!n) return;
      // Once something is confirmed, unmarked listings are no longer
      // protected - but say how many would go with it.
      var collateral = haveYes
        ? unmarked.filter(function (c) { return has(c, t); }).length : 0;
      exRows.push({
        tterm: t, term: rv[t], count: n,
        collateral: collateral, broad: !!isGeneric[t],
      });
    });
    exRows = dropRedundant(exRows, true);
    exRows.sort(function (a, b) {
      // Specific and free of collateral first: those are the safe defaults.
      return (a.broad ? 1 : 0) - (b.broad ? 1 : 0) ||
             a.collateral - b.collateral ||
             b.count - a.count || b.tterm.length - a.tterm.length;
    });
    res.excludes = exRows.slice(0, MAX_EXCLUDES).map(function (r) {
      return {
        term: r.term, tterm: r.tterm, count: r.count, broad: r.broad,
        collateral: r.collateral, all: r.count === rejected.length,
      };
    });

    /*
     * Require candidates: in every confirmed listing, in none rejected.
     *
     * Only confirmations qualify. A word common to everything you merely
     * did not reject is an accident of the result page, and turning that
     * into a hard gate is how an alert goes silent.
     */
    if (confirmed.length) {
      var kv = vocab(confirmed);
      var reqRows = [];
      Object.keys(kv).forEach(function (t) {
        // A require is a hard gate, so a word that describes any collectible
        // ("希少", "レア") must never become one.
        if (isRequired[t] || isGeneric[t]) return;
        if (!confirmed.every(function (c) { return has(c, t); })) return;
        if (rejected.some(function (c) { return has(c, t); })) return;
        reqRows.push({ tterm: t, term: kv[t], count: confirmed.length });
      });
      reqRows = dropRedundant(reqRows, false);   // a require wants specificity
      reqRows.sort(function (a, b) { return b.tterm.length - a.tterm.length; });
      res.requires = reqRows.slice(0, MAX_REQUIRES).map(function (r) {
        return { term: r.term, tterm: r.tterm, count: r.count };
      });
    }

    /* ---- threshold: only when scores already separate the two groups ---- */
    var sc = function (c) { return typeof c.score === "number" ? c.score : null; };
    var kScores = kept.map(sc).filter(function (x) { return x !== null; });
    var rScores = rejected.map(sc).filter(function (x) { return x !== null; });
    if (kScores.length && rScores.length) {
      var maxRej = Math.max.apply(null, rScores);
      var minKept = Math.min.apply(null, kScores);
      var cur = typeof opts.minScore === "number" ? opts.minScore : 0;
      if (maxRej < minKept) {
        // Land halfway between the two groups, rounded to a tidy 0.1.
        var v = Math.round(((maxRej + minKept) / 2) * 10) / 10;
        if (v > cur && v <= minKept) {
          res.threshold = { value: v, clears: minKept, blocks: maxRej };
        }
      }
    }

    /* ---- explain when there is nothing useful to propose ---- */
    if (!kept.length) {
      res.warnings.push(
        "You rejected everything, so there is nothing left to protect - any " +
        "word below would look safe. Usually that means the queries " +
        "themselves are wrong, not the filters.");
    } else if (!haveYes) {
      res.warnings.push(
        "Nothing is marked correct yet. Tick ✓ on the listings that ARE the " +
        "item and this can also work out what they have in common - which is " +
        "what stops the wrong ones far more reliably than excluding words " +
        "one at a time.");
    }
    if (haveYes && !res.excludes.length && !res.requires.length && !res.threshold) {
      res.warnings.push(
        "The rejected listings share no wording the confirmed ones avoid, and " +
        "their scores overlap. Mark a few more either way, or separate them " +
        "with a price limit instead.");
    }
    return res;
  }

  return { suggest: suggest, MAX_EXCLUDES: MAX_EXCLUDES, MAX_REQUIRES: MAX_REQUIRES };
});
