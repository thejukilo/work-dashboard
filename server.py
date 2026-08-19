#!/usr/bin/env python3
"""
Work Dashboard — one screen with your latest email, next appointment, last
Salesforce cases and newest chat messages.

    python3 server.py            # http://localhost:8766
    python3 server.py --demo     # sample data, no accounts needed

Everything runs locally: this process serves the page, holds the OAuth
redirect endpoints, and makes the API calls to Google and Salesforce
server-side (so the browser never needs a token and CORS never applies).

Env knobs:
    DASHBOARD_PORT     local port (default 8766)
    DASHBOARD_CONFIG   path to config.json
    DASHBOARD_TOKENS   path to tokens.json
    GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET
    SF_CLIENT_ID / SF_CLIENT_SECRET / SF_LOGIN_URL
"""

import json
import os
import secrets
import sys
import threading
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import auth
import demo
import httpclient as http
import sources

HERE = Path(__file__).resolve().parent
PORT = int(os.environ.get("DASHBOARD_PORT", "8766"))
DEMO_ONLY = "--demo" in sys.argv

STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
}

# state -> (provider, pkce_verifier); consumed once, on the callback.
_pending = {}
_pending_lock = threading.Lock()


def redirect_uri(provider):
    return f"http://localhost:{PORT}/auth/{provider}/callback"


# --------------------------------------------------------------------------- #
# Building the overview
# --------------------------------------------------------------------------- #
def section(fetch):
    """Run one feed, turning any failure into something the UI can render."""
    try:
        return {"ok": True, "items": fetch(), "error": None, "needs_auth": False}
    except auth.NeedsAuth as exc:
        return {"ok": False, "items": [], "error": str(exc), "needs_auth": True}
    except http.HttpError as exc:
        needs_auth = exc.status in (401, 403)
        return {"ok": False, "items": [], "error": str(exc), "needs_auth": needs_auth}
    except Exception as exc:                                  # never blank the page
        return {"ok": False, "items": [], "error": f"{type(exc).__name__}: {exc}",
                "needs_auth": False}


def build_overview(cfg):
    limits = cfg["limits"]

    def google(fetch, limit):
        def run():
            token = auth.credentials("google", cfg)["access_token"]
            return fetch(token, limit)
        return run

    def salesforce():
        creds = auth.credentials("salesforce", cfg)
        return sources.recent_cases(creds, cfg, limits["cases"])

    jobs = {
        "emails": google(sources.latest_emails, limits["emails"]),
        "events": google(sources.upcoming_events, limits["events"]),
        "chats": google(sources.latest_chats, limits["chats"]),
        "cases": salesforce,
    }
    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        results = dict(zip(jobs, pool.map(section, jobs.values())))

    return {
        "demo": False,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "sections": results,
    }


def build_status(cfg):
    return {
        "demo": False,
        "providers": {
            name: {"configured": auth.is_configured(name, cfg),
                   "connected": auth.is_connected(name)}
            for name in auth.PROVIDERS
        },
    }


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    server_version = "WorkDashboard/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("  %s\n" % (fmt % args))

    # -- helpers ------------------------------------------------------------ #
    def _send(self, code, body, ctype):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload, code=200):
        self._send(code, json.dumps(payload), "application/json; charset=utf-8")

    def _page(self, title, message, code=200):
        self._send(code, (
            "<!doctype html><meta charset='utf-8'>"
            "<title>%s</title>"
            "<body style=\"font:16px system-ui;background:#0e1a16;color:#eaf4ef;"
            "display:grid;place-items:center;height:100vh;margin:0;text-align:center\">"
            "<div><h1 style='font-size:1.3rem'>%s</h1><p style='color:#93ada3'>%s</p>"
            "<p><a style='color:#2bd4a0' href='/'>Back to the dashboard</a></p></div>"
        ) % (title, title, message), "text/html; charset=utf-8")

    def _static(self, route):
        filename, ctype = route
        try:
            body = (HERE / filename).read_bytes()
        except FileNotFoundError:
            self.send_error(404, f"{filename} not found next to server.py")
            return
        self._send(200, body, ctype)

    # -- OAuth -------------------------------------------------------------- #
    def _auth_start(self, provider, cfg):
        if not auth.is_configured(provider, cfg):
            self._page("Not configured yet",
                       f"Add a {provider} client_id to config.json, then restart.", 400)
            return
        state = secrets.token_urlsafe(24)
        verifier, challenge = auth.new_pkce()
        with _pending_lock:
            # Abandoned attempts (started, never finished) shouldn't pile up.
            for stale in list(_pending)[:-9]:
                _pending.pop(stale, None)
            _pending[state] = (provider, verifier)
        url = auth.authorize_url(provider, cfg, redirect_uri(provider), state, challenge)
        self.send_response(302)
        self.send_header("Location", url)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _auth_callback(self, provider, cfg, query):
        if query.get("error"):
            self._page("Connection cancelled",
                       query.get("error_description", [query["error"][0]])[0], 400)
            return
        state = (query.get("state") or [""])[0]
        code = (query.get("code") or [""])[0]
        with _pending_lock:
            pending = _pending.pop(state, None)
        if not pending or pending[0] != provider or not code:
            self._page("That link expired", "Start the connection again from the dashboard.", 400)
            return

        try:
            auth.exchange_code(provider, cfg, code, redirect_uri(provider), pending[1])
        except http.HttpError as exc:
            self._page("Could not finish connecting", str(exc), 502)
            return

        self.send_response(302)
        self.send_header("Location", "/")
        self.send_header("Content-Length", "0")
        self.end_headers()

    # -- routing ------------------------------------------------------------ #
    def do_GET(self):
        parsed = urlparse(self.path)
        path, query = parsed.path, parse_qs(parsed.query)
        wants_demo = DEMO_ONLY or "demo" in query

        if path in STATIC:
            self._static(STATIC[path])
            return

        cfg = auth.load_config()

        if path == "/api/overview":
            self._json(demo.overview() if wants_demo else build_overview(cfg))
            return
        if path == "/api/status":
            self._json(demo.status() if wants_demo else build_status(cfg))
            return

        parts = path.strip("/").split("/")
        if len(parts) == 3 and parts[0] == "auth" and parts[1] in auth.PROVIDERS:
            if DEMO_ONLY:
                self._page("Demo mode", "Restart without --demo to connect real accounts.", 400)
                return
            if parts[2] == "start":
                self._auth_start(parts[1], cfg)
                return
            if parts[2] == "callback":
                self._auth_callback(parts[1], cfg, query)
                return

        self.send_error(404)

    def do_POST(self):
        path = urlparse(self.path).path
        parts = path.strip("/").split("/")
        if len(parts) == 3 and parts[0] == "auth" and parts[1] in auth.PROVIDERS \
                and parts[2] == "disconnect":
            auth.forget(parts[1])
            self._json({"ok": True})
            return
        self.send_error(404)


# --------------------------------------------------------------------------- #
def main():
    cfg = auth.load_config()
    url = f"http://localhost:{PORT}/"

    print(f"Work Dashboard → {url}")
    if DEMO_ONLY:
        print("Demo mode: showing sample data, no accounts touched.")
    else:
        for provider in auth.PROVIDERS:
            if not auth.is_configured(provider, cfg):
                print(f"  [ ] {provider}: no client_id yet — see README.md")
            elif not auth.is_connected(provider):
                print(f"  [ ] {provider}: configured, not connected — click Connect in the app")
            else:
                print(f"  [x] {provider}: connected")
    print("Ctrl+C to stop.")

    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    try:
        threading.Timer(0.6, lambda: webbrowser.open(url + ("?demo" if DEMO_ONLY else ""))).start()
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
        server.shutdown()


if __name__ == "__main__":
    main()
