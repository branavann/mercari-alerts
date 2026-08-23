# Mercari Japan Alerts

A free, self-hosted replacement for paid Mercari alert services. You describe
an item once — ideally by showing it examples of past listings — and it emails
you when a new one appears.

Runs on GitHub Actions' free tier. No server, nothing running on your laptop.

---

## The problem this actually solves

Naive keyword alerts miss things, and here is the concrete reason. These are
four real listings of the **same** card (One Piece Hyper Battle promo C-E2):

```
A  ワンピース 旧 カードゲーム ルフィ海賊団 出航！ゴーイング・メリー号　F626
B  カードダス ワンピース ハイパーバトル プロモカード1枚
C  【美品】ワンピース ハイパーバトル カードダス C-E2 ルフィ海賊団
D  ワンピース 旧 カードゲーム ルフィ海賊団 出航！ゴーイング・メリー号　F650
```

They sold for ¥98,000 / ¥120,000 / ¥240,000 / ¥299,800 — so missing one is
expensive.

- Search `ハイパーバトル` → misses **A** and **D** (only in their descriptions).
- Search `ルフィ海賊団` → misses **B**.
- Search `C-E2` → only **C** has it in the title.
- Search `ワンピース` → catches all four, plus half a million other listings.

**No single keyword works.** So this tool doesn't use one. Each alert holds a
*fan-out* of queries plus a local scoring pass:

| stage | goal | how |
|---|---|---|
| **Queries** | recall | several differently-phrased searches, pooled and de-duplicated |
| **Match rules** | precision | weighted scoring over title **and description**, run locally |

Because the judging is strict, the queries can afford to be broad. That
inverts the usual tradeoff — you stop having to choose between "too noisy"
and "misses things".

---

## Teaching it an item

You don't write the rules by hand. You show it examples:

```bash
python learn_alert.py --name op-hyperbattle-ce2 \
    --label "One Piece Hyper Battle C-E2 (Going Merry)" \
    https://jp.mercari.com/item/m64572484257 \
    https://jp.mercari.com/item/m83971401198 \
    https://jp.mercari.com/item/m89240778353 \
    https://jp.mercari.com/item/m45169992781 \
    --not https://jp.mercari.com/item/<a-listing-you-do-not-want>
```

Sold listings are fine — the page stays up, and sold listings are usually the
easiest examples to find.

It fetches each one, pulls out the identity terms, **measures how common each
term is on Mercari**, and prints a report before writing anything:

```
QUERY FAN-OUT
  - C-E2                                (rare term, 45 listings)
  - ゴーイング・メリー号                     (rare term, 60 listings)
  - ルフィ海賊団                           (rare term, 140 listings)
  - ハイパーバトル                          (rare term, 900 listings)
  - ゴーイング・メリー号 ルフィ海賊団           (title of example 1)
  - ハイパーバトル カードダス                 (title of example 2)
  - C-E2 ルフィ海賊団                      (title of example 3)

RETRIEVAL COVERAGE   T = query matches the title alone,  d = only via description
  ex1  [dTTdTTdd]  ワンピース 旧 カードゲーム ルフィ海賊団 出航！ゴーイング・メリー号　F626
  ex2  [dddTddTd]  カードダス ワンピース ハイパーバトル プロモカード1枚
  ex3  [TdTTddTT]  【美品】ワンピース ハイパーバトル カードダス C-E2 ルフィ海賊団
  ex4  [dTTdTTdd]  ワンピース 旧 カードゲーム ルフィ海賊団 出航！ゴーイング・メリー号　F650

  OK  every example is reachable by title alone (the safe case).

TERM SPECIFICITY  (live Mercari result counts)
         45 listings   C-E2                   weight 13.04
         60 listings   ゴーイング・メリー号        weight 12.67
        900 listings   ハイパーバトル            weight 9.14
     21,000 listings   カードダス               weight 2.01
    480,000 listings   ワンピース               weight 0.38

SCORING   threshold = 19.3
  ex1   score    66.9   PASS      neg1  rejected by exclude 'ノーマル'
  ex2   score   60.72   PASS      neg2  rejected by exclude 'ノーマル'
  ex3   score    65.4   PASS      neg3  rejected by exclude '複製'
  ex4   score    66.9   PASS
```

The coverage grid is the useful part: it proves every example you gave it
would have been found. If a row came back all dots, the tool tells you so
instead of silently shipping a broken alert.

Add `--write` to append the result to `alerts.yaml`.

### Why measuring term rarity matters

`ワンピース` and `ゴーイング・メリー号` both appear in all four examples, so
pure frequency analysis rates them equally — and then a listing for a
*different* One Piece card scores just as high as the one you want. Asking
Mercari how many listings each term matches fixes that, and costs a handful of
API calls once, at learn time. It's why `ワンピース` ends up at weight 0.38 and
`C-E2` at 13.04.

---

## Setup

**1. Put this folder in a GitHub repo.** Public if you want the free hosted
control panel (see below) — note that makes your alert keywords and feed
history visible to anyone. Private works too; run the panel locally with
`python serve_ui.py`. Either way your Gmail secrets stay encrypted.

**2. Gmail app password.** Turn on 2-Step Verification, then go to
https://myaccount.google.com/apppasswords and create one. Copy the 16
characters, no spaces.

**3. Repo secrets** (Settings → Secrets and variables → Actions):

| Secret | Value |
|---|---|
| `GMAIL_USER` | your Gmail address |
| `GMAIL_APP_PASSWORD` | the app password from step 2 |
| `ALERT_TO` | where alerts go — same address, or a comma-separated list |

**4. Create your alerts** in the control panel, with `learn_alert.py`, or by
editing `alerts.yaml` by hand.

**5. Test before going live:**

```bash
pip install -r requirements.txt
python mercari_alert.py --dry-run --only op-hyperbattle-ce2 --explain
```

`--dry-run` scores everything currently listed and prints the result without
emailing or saving state. `--explain` shows the score for every candidate and
why it passed or failed. Iterate here until you like what you see — this is
much faster than waiting for scheduled runs.

**6. Enable the workflow.** Actions tab → "Mercari Alerts" → Run workflow.
Check the log, then leave it to its schedule. Public repos get unlimited
free Actions minutes, so you can raise the frequency if you want.

The first run of any alert **baselines silently** — it records what's already
listed and emails nothing, so adding an alert doesn't flood you. You get
emails about listings that appear after that.

---

## What else is in here

Things worth knowing about beyond keyword matching:

**Descriptions are searched, not just titles.** Sellers routinely bury the
card code, the issue number, or the platform in the description body. This is
the single biggest recall win, and it's what the paid services charge extra
for as "deep search". Descriptions are only fetched for listings whose title
can't decide the outcome, and never for listings already evaluated on a
previous run — so it stays cheap.

**Text normalisation that matches how Japanese listings are actually
written.** `C-E2` = `CE-2` = `CE2` = `Ｃ－Ｅ２`; `ゴーイング・メリー号` =
`ゴーイングメリー号`; full-width and half-width, katakana and hiragana all
fold together. Short codes still respect word boundaries, so `F626` won't
match inside `AF6261`.

**Sold-price comps.** `python comps.py <alert-name>` runs the alert's queries
against *sold* listings, applies the same match rules, and reports the real
distribution — so you know whether ¥98,000 is a steal or a rip-off, and it
suggests a `price_max`. Listings priced well below your rolling median get a
**below median** badge in the email.

**Relist detection.** Sellers delete and re-post items to bump them up the
feed, which mints a fresh item ID and re-triggers naive alerts. Items are
fingerprinted on normalised title + price bucket + seller, so a repost is
labelled as a relist (or suppressed entirely with `suppress_relists: true`).

**A "possible matches" section.** Anything scoring at least 55% of the
threshold appears in a second section of the email rather than disappearing.
Near-misses are exactly the feedback you need to tune a rule, and silently
dropped ones teach you nothing.

**Sane defaults for junk.** Reproductions, want-to-buy posts, "box only" and
"manual only" listings are excluded for every alert automatically. Opt-in
bundles — `no_junk`, `no_bulk`, `no_graded`, `no_parts` — cover the rest.

**Per-alert pacing.** `min_interval_minutes` lets narrow alerts run every 15
minutes while broad ones check hourly, without splitting the workflow.

**Tests that run before every check.** The workflow runs 45 offline tests
against real listing fixtures first. If the matching engine breaks, you find
out from a red run rather than from months of quiet inboxes.

---

## The control panel

A web UI for managing all of this, styled after the paid services but running
entirely on GitHub's free tier.

**Feed** — every match, newest first, with price in ¥ and approximate $, how
long after listing it was spotted, why it matched, and badges for
below-median / at-your-target / relisted. Filter by alert, by confidence, or
by title.

**Alerts** — pause, resume, edit, delete. Shows 24 h match count, median
price and last check time per alert.

**New alert** — a four-step wizard: Search → Filters → Preview → Delivery.

The wizard's first step is the important one. It offers two modes:

- **Learn from example listings** — paste 2-4 past listings of the item and
  it derives the whole strategy: the query fan-out, weighted signals, the
  score threshold, and a coverage report proving each example is reachable.
  This is the same engine `learn_alert.py` uses, just driven from a browser.
- **Type search terms** — enter keywords yourself, with a Japanese
  translation suggested as you type.

**Preview** answers "what would this actually catch?" before you commit —
it runs your queries against live Mercari, scores everything they return,
and shows the matches, the per-query breakdown, and the near-misses.

### Publishing it

The repo needs to be **public** for free GitHub Pages. Then:

Settings → Pages → Source: **Deploy from a branch** → `main` → `/ (root)` → Save.

Your panel is at `https://<you>.github.io/<repo>/ui/` within a minute or two.
It republishes automatically every time the checker commits new results.

Prefer to keep it private? `python serve_ui.py` serves the identical UI at
`localhost:8765` — every feature works the same.

### Connecting it

Reading the feed needs nothing. Saving alerts and running previews need a
token, since those write to your repo:

1. github.com/settings/personal-access-tokens → **Generate new token (fine-grained)**
2. Repository access → **Only select repositories** → this repo
3. Permissions → Repository → **Contents: Read and write**, **Actions: Read and write**
4. Paste it into the panel's Settings page.

The token lives in your browser's localStorage and is sent only to
api.github.com. **Never commit it** — on a public repo that would expose it
to everyone. Your Gmail secrets are unaffected: repository secrets stay
encrypted and invisible even on a public repo.

### How a static page runs live searches

It can't, directly — browsers are blocked by CORS and Mercari's API needs
request signing. So the page dispatches a workflow and polls for its result:

```
  browser ──dispatch──▶ ui_preview.yml ──▶ queries Mercari, scores results
     ▲                                              │
     └──────── polls ui/data/previews/<id>.json ◀───┘ commits result
```

`ui_learn.yml` works the same way for learning from examples. Each takes
roughly 40-90 seconds. The scheduled checker writes `ui/data/feed.json`,
which is what the Feed page reads — no database, and the feed has history
because it's in git.

### Translation

English search terms are translated two ways. A built-in dictionary handles
collectible vocabulary that general machine translation gets wrong —
platform names, card and magazine terms, One Piece character names,
condition words. Anything it doesn't know falls through to
[MyMemory](https://mymemory.translated.net/), which is free and needs no key
(5,000 characters a day, or 50,000 if you put an email in Settings).

Treat both as suggestions. The most accurate Japanese comes from the
learn-from-examples flow, because it extracts the words sellers actually
used.

### Where alerts live

| File | Owner | Notes |
|---|---|---|
| `alerts.json` | the web UI | machine-written, safe to rewrite |
| `alerts.yaml` | you | hand-editable, keeps comments |

Both load, both run; names must be unique across the two.

---

## Tuning cheat-sheet

| symptom | fix |
|---|---|
| missing listings you found manually | add that listing as an example and re-run `learn_alert.py`; or add a query built from its title |
| too many wrong items | add them with `--not` and re-learn, or raise `min_score` |
| right item keeps landing in "possible matches" | lower `min_score` |
| one word keeps ruining results | add it to `match.exclude` |
| alert is too slow / too chatty | `min_interval_minutes`, `price_min` / `price_max` |
| no idea why something matched | `--explain`, or the Preview step in the UI |

Adding an exclude is the bluntest tool available — a wrong one silently kills
real hits forever. Prefer raising `min_score` first.

---

## Limitations, honestly

- **It rides an unofficial API.** [`mercapi`](https://github.com/take-kun/mercapi)
  reverse-engineers Mercari's app API; there is no public one. Mercari can
  change or block it at any time, which would break this until the library is
  updated — or permanently. If alerts stop, check the Actions logs first.
  You are maintaining a hobby project, not subscribing to a monitored service.
- **Terms of service.** Automated access almost certainly isn't covered by
  Mercari's terms. Keep the interval reasonable and the volume personal.
- **No image matching yet.** Mercari has no reverse-image API, so this would
  mean running a visual-similarity model over every candidate's photos. It's
  a real phase two, not a small addition. The current engine already handles
  the cases image search is usually reached for — unnamed items get caught by
  their code, character name, or year instead.
- **Japan only.** Mercari US is a different site and API.
- **Not instant.** GitHub's scheduler drifts by a few minutes. For genuinely
  contested items a paid service with sub-minute polling will beat this.
- **Negation isn't understood.** A listing saying "C-E2ではありません"
  ("this is not C-E2") contains the term and can score highly. That's what
  `--not` examples and exclude terms are for.

## Cost

Zero. Private repos get 2,000 free Actions minutes/month; each run takes well
under a minute. Gmail sending is free at this volume.

## Layout

```
mercari_alert.py     the scheduled runner
learn_alert.py       build an alert from example listings (CLI)
comps.py             sold-price statistics for an alert
serve_ui.py          serve the control panel locally
alerts.json          alerts owned by the web UI
alerts.yaml          hand-written alerts
ui/
  index.html         the whole control panel, one file, no build step
  data/              feed.json, status.json, preview + learn results
tools/ui_task.py     the preview / learn jobs the UI dispatches
mlert/
  textutil.py        Japanese normalisation + term extraction
  rules.py           scoring engine
  learn.py           learning from examples
  mercari.py         the only file that touches the network
  state.py           seen-items, price history, relist fingerprints
  feed.py            the JSON files the UI reads
  notify.py          email composition + Gmail SMTP
  config.py          alerts.json + alerts.yaml loading
tests/               offline tests against real listing fixtures
.github/workflows/
  check_alerts.yml   scheduled checker
  ui_preview.yml     dispatched by the UI
  ui_learn.yml       dispatched by the UI
```
