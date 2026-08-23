#!/usr/bin/env python3
"""
Serve the control panel locally.

    python serve_ui.py

Useful when you want to work on alerts without pushing, or if you'd rather
not publish the site at all. Everything works the same as the hosted copy -
it talks to GitHub over the API either way.
"""

import argparse
import http.server
import socketserver
import threading
import webbrowser
from functools import partial
from pathlib import Path

ROOT = Path(__file__).resolve().parent


class Handler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        # The page polls committed JSON; caching it would show stale results.
        self.send_header("Cache-Control", "no-store, max-age=0")
        super().end_headers()

    def log_message(self, fmt, *args):
        if "GET /ui" in (fmt % args) or "500" in (fmt % args):
            super().log_message(fmt, *args)


def main():
    p = argparse.ArgumentParser(description="Serve the Mercari Alerts UI locally.")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-open", action="store_true")
    args = p.parse_args()

    handler = partial(Handler, directory=str(ROOT))
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("127.0.0.1", args.port), handler) as httpd:
        url = f"http://127.0.0.1:{args.port}/ui/"
        print(f"\n  Mercari Alerts  ->  {url}\n  (ctrl-C to stop)\n")
        if not args.no_open:
            threading.Timer(0.6, lambda: webbrowser.open(url)).start()
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nstopped")


if __name__ == "__main__":
    main()
