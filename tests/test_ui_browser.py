"""
Browser test for the reject-and-refine loop in ui/index.html.

    python tests/test_ui_browser.py [--headed] [--shots DIR]

Serves ui/ over http, stubs api.github.com so no network or token is needed,
drives the real wizard, and asserts on the resulting DOM. Playwright is not a
requirement of this project - if it isn't installed the test skips cleanly, so
it can sit in CI without forcing the install.
"""

import argparse
import base64
import functools
import http.server
import json
import socketserver
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("playwright not installed - skipping browser test")
    raise SystemExit(0)

FIXTURE = ROOT / "tests" / "fixtures" / "preview_result.json"

SETTINGS = {
    "owner": "branavann", "repo": "mercari-alerts", "branch": "main",
    "token": "ghp_test", "jpy": 150, "autoTranslate": False,
}


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


def serve(directory):
    handler = functools.partial(Quiet, directory=str(directory))
    httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, httpd.server_address[1]


class Checks:
    def __init__(self):
        self.failed = 0

    def __call__(self, name, cond, detail=""):
        if cond:
            print(f"  PASS  {name}")
        else:
            self.failed += 1
            print(f"  FAIL  {name}{': ' + str(detail) if detail else ''}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--shots", help="directory to write screenshots into")
    args = ap.parse_args()

    if not FIXTURE.exists():
        raise SystemExit(f"missing {FIXTURE} - run tests/make_preview_fixture.py")
    preview = json.loads(FIXTURE.read_text("utf-8"))

    # Serve the repo root, not ui/, because the page reaches up to
    # ../alerts.json exactly as it does on GitHub Pages.
    httpd, port = serve(ROOT)
    base = f"http://127.0.0.1:{port}/ui/index.html"
    check = Checks()
    errors = []

    def shot(page, name):
        if args.shots:
            d = Path(args.shots)
            d.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(d / name), full_page=True)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not args.headed)
        page = browser.new_page(viewport={"width": 1280, "height": 1000})
        page.on("console", lambda m: m.type == "error" and errors.append(m.text))
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))

        # Stub GitHub: the dispatch succeeds, the polled result is our
        # fixture, and every other Contents read is served from the working
        # tree so the page sees a realistic repo.
        def gh_route(route):
            url = route.request.url

            def contents(payload):
                blob = base64.b64encode(payload.encode()).decode()
                return route.fulfill(
                    status=200, content_type="application/json",
                    body=json.dumps({"sha": "abc", "content": blob}))

            if "/dispatches" in url:
                return route.fulfill(status=204, body="")
            if "/contents/ui/data/previews/" in url:
                return contents(json.dumps(preview, ensure_ascii=False))
            if "/contents/" in url:
                rel = url.split("/contents/", 1)[1].split("?")[0]
                f = ROOT / rel
                if f.is_file():
                    return contents(f.read_text("utf-8"))
            return route.fulfill(status=404, body="{}")

        page.route("https://api.github.com/**", gh_route)

        # Stub the translation service: no network here, and a canned answer
        # lets the English glossary be tested for real.
        def mm_route(route):
            return route.fulfill(
                status=200, content_type="application/json",
                body=json.dumps({"responseStatus": 200,
                                 "responseData": {"translatedText": "dawn of adventure"}}))

        page.route("https://api.mymemory.translated.net/**", mm_route)
        page.add_init_script(
            "localStorage.setItem('mercari-alerts.settings.v1', %s)"
            % json.dumps(json.dumps(SETTINGS))
        )

        page.goto(base)
        page.wait_for_function("typeof window.MREFINE === 'object'")
        check("refine.js loads alongside the page", True)

        # Enter the wizard directly at the preview step with a manual draft.
        page.evaluate("location.hash = '#/new'")
        page.wait_for_timeout(300)
        page.evaluate("""() => {
            Object.assign(W, {mode:'manual', step:3, name:'op-ce2',
                              label:'One Piece C-E2',
                              queries:['ハイパーバトル ルフィ海賊団']});
            viewWizard();
        }""")
        page.click("text=Run preview")
        page.wait_for_selector(".lc", timeout=25000)

        n = page.locator(".lc").count()
        check("preview renders every card", n == 6, f"got {n}")
        shot(page, "1-preview.png")

        # Confirm the two that ARE 冒険を求めて; reject the other cards.
        for item_id in ("m1", "m2"):
            page.locator(f'.lc:has(a[href$="/{item_id}"]) button.jm.yes').click()
        for item_id in ("m3", "m4", "m5", "m6"):
            page.locator(f'.lc:has(a[href$="/{item_id}"]) button.jm.no').click()
        check("confirmed cards are visibly marked", page.locator(".lc.yes").count() == 2)
        check("rejected cards are visibly marked", page.locator(".lc.rej").count() == 4)
        check("the refine bar appears", page.locator(".refinebar").is_visible())

        page.click("text=Suggest filters")
        page.wait_for_selector(".refpanel")
        shot(page, "2-suggestions.png")

        props = page.locator(".refpanel .prop .t").all_text_contents()
        joined = " | ".join(props)

        # The whole point: 冒険を求めて must be separable from 冒険の夜明け,
        # which is only possible now that phrases survive their hiragana.
        # 夜明 rather than the whole 冒険の夜明け is the better answer: the
        # shorter word is absent from the wanted card just the same, and it
        # also catches the next seller who writes the name differently.
        check("the rival card is separated by its own word",
              any("夜明" in p for p in props), joined)
        check("the bulk lot gets its own exclude",
              any("まとめ" in p or "300枚" in p for p in props), joined)
        check("the shared word 冒険 is never offered on its own",
              "冒険" not in [p.strip() for p in props], joined)
        check("the wanted card name is never offered as an exclude",
              not any("冒険を求めて" in p for p in props), joined)
        check("family words are never proposed",
              not ({"ハイパーバトル", "ルフィ海賊団", "ワンピース"} & set(props)), joined)
        # 冒険を求めて cannot be required here: the rejected bulk lot is the
        # same card, so the name does not separate the two groups. Proposing
        # it anyway would be wrong, and the panel correctly declines.
        check("no requirement is invented when the name doesn't separate",
              page.locator(".refpanel h3", has_text="require").count() == 0)

        page.click("text=Apply without re-running")
        page.wait_for_timeout(250)
        draft = page.evaluate("() => ({exclude:W.exclude, marks:W.marks})")
        check("the exclude reaches the draft",
              any("夜明" in x for x in draft["exclude"]), draft["exclude"])
        rejected = [k for k, v in draft["marks"].items() if v == "no"]
        check("rejected ids are remembered", len(rejected) == 4, rejected)

        # The save summary must disclose what is being written.
        page.evaluate("() => { W.step = 4; viewWizard(); }")
        page.wait_for_timeout(250)
        summary = page.locator(".card table").first.inner_text()
        check("the summary discloses the dismissals",
              "4 — never shown again" in summary, summary[:200])
        shot(page, "3-summary.png")

        # English glosses: the curated dictionary must answer without any
        # network call, and be visible next to the Japanese.
        page.evaluate("() => { W.step = 3; viewWizard(); }")
        page.wait_for_timeout(200)
        page.click("text=Suggest filters")
        page.wait_for_selector(".refpanel")
        page.wait_for_timeout(600)          # let the async lookups settle
        en = page.locator(".refpanel .prop .t .en").all_text_contents()
        check("Japanese proposals carry an English gloss", len(en) > 0, en)
        check("the curated dictionary is preferred over machine translation",
              page.evaluate("englishFor('まとめ')") == "bulk lot",
              page.evaluate("englishFor('まとめ')"))
        check("English is off when the setting is off",
              page.evaluate("() => { S.showEn=false; return gloss('まとめ'); }") is None)
        page.evaluate("() => { S.showEn=true; }")

        # The sticky refine bar must not break the phone layout.
        page.set_viewport_size({"width": 390, "height": 844})
        page.evaluate("() => { W.step = 3; viewWizard(); }")
        page.wait_for_timeout(400)
        overflow = page.evaluate(
            "document.documentElement.scrollWidth - document.documentElement.clientWidth")
        check("no horizontal overflow on mobile", overflow <= 1, f"{overflow}px")
        shot(page, "4-mobile.png")

        # The other views share listingCard; make sure the new reject button
        # stayed opt-in and did not leak into the feed.
        page.set_viewport_size({"width": 1280, "height": 1000})
        for route_hash, name in (("#/feed", "Feed"), ("#/alerts", "Alerts"),
                                 ("#/settings", "Settings")):
            page.evaluate(f"location.hash = '{route_hash}'")
            page.wait_for_timeout(350)
            check(f"{name} still renders", page.locator("#view *").count() > 0)
        check("no reject buttons outside the preview",
              page.locator("button.rej").count() == 0)

        check("no console errors", not errors, " | ".join(errors))
        browser.close()

    httpd.shutdown()
    print(f"\n{'all passed' if not check.failed else str(check.failed) + ' failed'}")
    return 1 if check.failed else 0


if __name__ == "__main__":
    sys.exit(main())
