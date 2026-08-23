/*
 * Tests for ui/refine.js - the reject-and-refine suggestion engine.
 *
 *     node tests/test_refine_js.mjs
 *
 * The fixtures use the real shape the preview task emits: `tt` is the whole
 * listing tight-normalised, `tterms` are its candidate terms in the same
 * form. Both come from mlert/refine.py, which tests/test_refine.py covers.
 */

import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import path from "node:path";

const require = createRequire(import.meta.url);
const ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const { suggest } = require(path.join(ROOT, "ui", "refine.js"));

let failed = 0;
function test(name, fn) {
  try { fn(); console.log(`  PASS  ${name}`); }
  catch (e) { failed++; console.log(`  FAIL  ${name}: ${e.message}`); }
}
function eq(a, b, msg) {
  const [x, y] = [JSON.stringify(a), JSON.stringify(b)];
  if (x !== y) throw new Error(`${msg || ""} expected ${y}, got ${x}`);
}
function ok(v, msg) { if (!v) throw new Error(msg || "expected truthy"); }

/* A card, built the way ui_task.py does: terms are substrings of tt. */
function card(id, score, terms, generic) {
  return { id, score, terms, tterms: terms, generic: generic || [], tt: terms.join("") };
}

// The real situation from the screenshot: same card family, different cards.
const WANTED  = card("m1", 30, ["ごーいんぐめりー", "るふぃかいぞくだん", "はいぱーばとる", "ce2"]);
const VACANCE = card("m2", 23, ["ばかんす", "るふぃかいぞくだん", "はいぱーばとる"]);
const HANABI  = card("m3", 23, ["はなびけんぶつ", "るふぃかいぞくだん", "はいぱーばとる"]);

// --------------------------------------------------------------------------

test("no rejections proposes nothing", () => {
  const r = suggest({confirmed: [WANTED], rejected: []}, {}, {});
  eq(r.excludes, []); eq(r.requires, []); eq(r.threshold, null);
});

test("a word unique to the rejected listings becomes an exclude", () => {
  const r = suggest({confirmed: [WANTED], rejected: [VACANCE]}, {}, {});
  eq(r.excludes.map(x => x.term), ["ばかんす"]);
  ok(r.excludes[0].all, "single rejection should be flagged as covering all");
});

test("a word shared with a kept listing is never proposed", () => {
  const r = suggest({confirmed: [WANTED], rejected: [VACANCE, HANABI]}, {}, {});
  const terms = r.excludes.map(x => x.term);
  ok(!terms.includes("はいぱーばとる"), "family term must not be excludable");
  ok(!terms.includes("るふぃかいぞくだん"), "shared term must not be excludable");
  eq(terms.sort(), ["はなびけんぶつ", "ばかんす"]);
});

test("excludes rank by how many rejections they cover", () => {
  const a = card("a", 20, ["じゃんく", "きず"]);
  const b = card("b", 20, ["じゃんく"]);
  const r = suggest({confirmed: [WANTED], rejected: [a, b]}, {}, {});
  eq(r.excludes[0].term, "じゃんく");
  eq(r.excludes[0].count, 2);
  eq(r.excludes[0].all, true);
});

test("the broader of two nested exclude words wins", () => {
  // "まとめ売り" covers "3枚まとめ売り"; the shorter one also catches the
  // next seller who words it differently.
  const a = card("a", 20, ["まとめうり", "3まいまとめうり"]);
  const r = suggest({confirmed: [WANTED], rejected: [a]}, {}, {});
  eq(r.excludes.map(x => x.term), ["まとめうり"]);
});

test("a word already excluded or required is not re-proposed", () => {
  const cur = { exclude: ["ばかんす"], require: [["はなびけんぶつ"]] };
  const r = suggest({confirmed: [WANTED], rejected: [VACANCE, HANABI]}, cur, {});
  eq(r.excludes.map(x => x.term), []);
});

test("a word unique to every kept listing becomes a require", () => {
  const other = card("m4", 31, ["ごーいんぐめりー", "はいぱーばとる", "ce2"]);
  const r = suggest({confirmed: [WANTED, other], rejected: [VACANCE, HANABI]}, {}, {});
  const terms = r.requires.map(x => x.term);
  ok(terms.includes("ごーいんぐめりー"), `got ${terms}`);
  ok(!terms.includes("はいぱーばとる"), "term shared with rejected must not be required");
});

test("requires prefer the more specific of two nested words", () => {
  const k = card("k", 30, ["めりーごう", "めりー"]);
  const r = suggest({confirmed: [k], rejected: [VACANCE]}, {}, {});
  eq(r.requires.map(x => x.term), ["めりーごう"]);
});

test("a rival name is excludable even though it contains an alert word", () => {
  // 冒険の夜明け contains the signal 冒険, but 夜明け is a different card and
  // excluding it is the entire point of the exercise.
  const cur = { require: [["わんぴーす"]], signals: { "冒険": 4 } };
  const want = card("w", 30, ["冒険を求めて", "冒険"]);
  const dawn = card("d", 30, ["冒険の夜明け", "冒険"]);
  const r = suggest({ confirmed: [want], rejected: [dawn] }, cur, {});
  ok(r.excludes.some(x => x.term === "冒険の夜明け"), r.excludes.map(x => x.term));
  ok(!r.excludes.some(x => x.term === "冒険"), "the shared word must stay");
});

test("a compound built from the alert's own words is never excludable", () => {
  // "ワンピースカードダス" is absent from the kept listings only because those
  // sellers put a space in it. Excluding it would bin the next real listing
  // that runs the words together.
  const cur = { require: [["わんぴーす"]], signals: { "かーどだす": 4 } };
  const rej = card("r", 20, ["わんぴーすかーどだす", "ばかんす"]);
  const r = suggest({confirmed: [WANTED], rejected: [rej]}, cur, {});
  eq(r.excludes.map(x => x.term), ["ばかんす"]);
});

test("broad words are offered but flagged, and sorted last", () => {
  const rej = card("r", 20, ["ばかんす", "れあ", "せっと"], ["れあ", "せっと"]);
  const r = suggest({confirmed: [WANTED], rejected: [rej]}, {}, {});
  eq(r.excludes[0].term, "ばかんす");
  eq(r.excludes[0].broad, false);
  const broad = r.excludes.filter(x => x.broad).map(x => x.term).sort();
  eq(broad, ["せっと", "れあ"]);
});

test("a broad word is never proposed as a requirement", () => {
  const k = card("k", 30, ["きしょう", "めりーごう"], ["きしょう"]);
  const r = suggest({confirmed: [k], rejected: [VACANCE]}, {}, {});
  eq(r.requires.map(x => x.term), ["めりーごう"]);
});

test("threshold is proposed only when the scores actually separate", () => {
  const r = suggest({confirmed: [WANTED], rejected: [VACANCE, HANABI]}, {}, { minScore: 10 });
  ok(r.threshold, "23 < 30 should separate");
  eq(r.threshold.value, 26.5);
  eq(r.threshold.blocks, 23);
  eq(r.threshold.clears, 30);
});

test("no threshold when a rejected listing outscores a kept one", () => {
  const hot = card("h", 40, ["ばかんす"]);
  eq(suggest({confirmed: [WANTED], rejected: [hot]}, {}, { minScore: 10 }).threshold, null);
});

test("no threshold when it would not raise the current one", () => {
  eq(suggest({confirmed: [WANTED], rejected: [VACANCE]}, {}, { minScore: 99 }).threshold, null);
});

test("rejecting everything warns instead of proposing", () => {
  const r = suggest({confirmed: [], rejected: [VACANCE, HANABI]}, {}, {});
  eq(r.keptCount, 0);
  ok(r.warnings.length, "should warn that nothing is being protected");
});

test("indistinguishable listings say so rather than inventing a filter", () => {
  const a = card("a", 20, ["おなじ"]);
  const b = card("b", 20, ["おなじ"]);
  const r = suggest({confirmed: [a], rejected: [b]}, {}, { minScore: 10 });
  eq(r.excludes, []); eq(r.requires, []); eq(r.threshold, null);
  ok(r.warnings.length, "should explain why there is nothing to suggest");
});

/* ---- confirmation changes what counts as evidence ---------------------- */

test("unmarked listings alone can still yield excludes", () => {
  // Nothing confirmed: "not rejected" is the best evidence there is.
  const r = suggest({ confirmed: [], rejected: [VACANCE], unmarked: [WANTED] }, {}, {});
  eq(r.excludes.map(x => x.term), ["ばかんす"]);
  eq(r.confirmedCount, 0);
  ok(r.warnings.some(w => /marked correct/.test(w)), "should nudge toward confirming");
});

test("without a confirmation no requirement is ever proposed", () => {
  // A word common to everything you merely didn't reject is an accident of
  // the result page; making it a hard gate is how an alert goes silent.
  const other = card("m4", 31, ["ごーいんぐめりー", "はいぱーばとる", "ce2"]);
  const r = suggest({ confirmed: [], rejected: [VACANCE], unmarked: [WANTED, other] }, {}, {});
  eq(r.requires, []);
});

test("confirming makes unmarked listings fair game, but counts the cost", () => {
  const bulk = card("b", 20, ["まとめ", "ばかんす"]);
  const alsoBulk = card("u", 20, ["まとめ", "なにか"]);
  const r = suggest({ confirmed: [WANTED], rejected: [bulk], unmarked: [alsoBulk] }, {}, {});
  const matome = r.excludes.find(x => x.term === "まとめ");
  ok(matome, "a word only in unmarked+rejected should now be offerable");
  eq(matome.collateral, 1, "and must disclose the unmarked listing it drops");
  eq(r.excludes.find(x => x.term === "ばかんす").collateral, 0);
});

test("collateral-free excludes sort ahead of costly ones", () => {
  const bulk = card("b", 20, ["まとめ", "ばかんす"]);
  const alsoBulk = card("u", 20, ["まとめ"]);
  const r = suggest({ confirmed: [WANTED], rejected: [bulk], unmarked: [alsoBulk] }, {}, {});
  eq(r.excludes[0].term, "ばかんす");
});

test("the real card-name case separates once confirmed", () => {
  // 冒険を求めて vs 冒険の夜明け: only bridged phrases tell them apart, and
  // only a confirmation turns the right one into a requirement.
  const want1 = card("w1", 30, ["冒険を求めて", "るふぃかいぞくだん", "冒険"]);
  const want2 = card("w2", 29, ["冒険を求めて", "るふぃかいぞくだん", "冒険"]);
  const dawn = card("d", 30, ["冒険の夜明け", "るふぃかいぞくだん", "冒険"]);
  const r = suggest({ confirmed: [want1, want2], rejected: [dawn], unmarked: [] }, {}, {});
  eq(r.excludes.map(x => x.term), ["冒険の夜明け"]);
  eq(r.requires.map(x => x.term), ["冒険を求めて"]);
});

test("missing or malformed cards do not throw", () => {
  const r = suggest({confirmed: [{ id: "x" }],
                     rejected: [{ id: "y", terms: ["あ"], tterms: ["あ"] }]}, null, null);
  ok(Array.isArray(r.excludes));
});

console.log(failed ? `\n${failed} failed` : "\nall passed");
process.exit(failed ? 1 : 0);
