#!/usr/bin/env python3
"""
Work Dashboard — one screen with your latest email, next appointment, last
Salesforce cases and newest chat messages.

    python3 server.py            # http://localhost:8766
    python3 server.py --demo     # sample data, no accounts needed
    python3 server.py --check     # diagnose setup: config, connection, live calls

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
import chrome_source
import demo
import httpclient as http
import scraper
import sources

HERE = Path(__file__).resolve().parent
PORT = int(os.environ.get("DASHBOARD_PORT", "8766"))
DEMO_ONLY = "--demo" in sys.argv
CHECK_ONLY = "--check" in sys.argv

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
SECTION_FEATURES = {"emails": "gmail", "events": "calendar", "chats": "chat"}


def section(fetch):
    """Run one feed, turning any failure into something the UI can render."""
    try:
        return {"ok": True, "items": fetch(), "error": None, "needs_auth": False}
    except auth.NeedsAuth as exc:
        return {"ok": False, "items": [], "error": str(exc), "needs_auth": True}
    except http.HttpError as exc:
        return {"ok": False, "items": [], "error": str(exc),
                "needs_auth": exc.status == 401}
    except scraper.ScrapeError as exc:      # browser-scrape source: log in / retune
        return {"ok": False, "items": [], "error": str(exc), "needs_auth": False}
    except Exception as exc:                                  # never blank the page
        return {"ok": False, "items": [], "error": f"{type(exc).__name__}: {exc}",
                "needs_auth": False}


def switched_off(feature):
    return {"ok": False, "items": [], "off": True, "needs_auth": False,
            "error": f"Turned off — add \"{feature}\" to google.features in config.json."}


def build_overview(cfg):
    limits = cfg["limits"]
    features = auth.google_features(cfg)

    def google(feature, fetch, limit):
        def run():
            token = auth.credentials("google", cfg)["access_token"]
            return fetch(token, limit)
        return run if feature in features else None

    def salesforce():
        creds = auth.credentials("salesforce", cfg)
        return sources.recent_cases(creds, cfg, limits["cases"])

    # Opt-in browser sources, for orgs whose OAuth path is blocked. Two backends
    # read a real logged-in browser instead of the APIs:
    #   "chrome" — your already-running, CAA-verified Chrome (chrome_source)
    #   "scrape" — a browser Playwright launches (scraper)
    # Everything else stays on the sanctioned API path.
    def emails_job():
        src = cfg["google"].get("email_source")
        if src == "chrome":
            return lambda: chrome_source.latest_emails(limits["emails"])
        if src == "scrape":
            return lambda: scraper.latest_emails(limits["emails"])
        return google("gmail", sources.latest_emails, limits["emails"])

    def cases_job():
        src = cfg["salesforce"].get("case_source")
        instance = cfg["salesforce"].get("instance_url", "")
        if src == "chrome":
            return lambda: chrome_source.recent_cases(instance, limits["cases"])
        if src == "scrape":
            return lambda: scraper.recent_cases(instance, limits["cases"])
        return salesforce

    jobs = {
        "emails": emails_job(),
        "events": google("calendar", sources.upcoming_events, limits["events"]),
        "chats": google("chat", sources.latest_chats, limits["chats"]),
        "cases": cases_job(),
    }
    live = {name: job for name, job in jobs.items() if job}
    with ThreadPoolExecutor(max_workers=max(1, len(live))) as pool:
        done = dict(zip(live, pool.map(section, live.values())))

    results = {name: done.get(name) or switched_off(SECTION_FEATURES[name])
               for name in jobs}

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
            code = query["error"][0]
            detail = query.get("error_description", [code])[0]
            self._page("Connection cancelled",
                       detail + consent_hint(code, provider, cfg), 400)
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
# Setup doctor (--check)
# --------------------------------------------------------------------------- #
FEED_LABELS = {
    "emails": ("Gmail", "google"),
    "events": ("Calendar", "google"),
    "chats": ("Chat", "google"),
    "cases": ("Salesforce cases", "salesforce"),
}

# Failure text → the thing that actually needs fixing. Ordered: first match wins.
HINTS = [
    ("has not been used in project", "Enable that API in the Google Cloud console, wait a minute, retry."),
    ("access_not_configured", "Either that API isn't enabled on the client id's project, or your org "
                              "blocks the scope — connect one google.features entry at a time to tell which."),
    ("api has not been enabled", "Enable that API in the Google Cloud console, wait a minute, retry."),
    ("insufficient authentication scopes", "Scope missing from the grant — add it on the consent screen, then reconnect."),
    ("insufficient permission", "Scope missing from the grant — add it on the consent screen, then reconnect."),
    ("caller does not have permission", "Your Workspace admin may not allow this API for user accounts."),
    ("admin_policy_enforced", "Your Workspace admin must trust this OAuth client id before it can read the data."),
    ("access_denied", "Consent was refused — by you, or by an organisation policy on the account."),
    ("invalid_session_id", "Salesforce session expired — reconnect from the dashboard."),
    ("invalid_client", "Check the client id/secret, and give a new connected app ~10 minutes to propagate."),
    ("invalid_grant", "The stored grant is no longer valid — reconnect from the dashboard."),
    ("no instance_url", "Reconnect Salesforce; the stored token predates the instance URL."),
]


def consent_hint(error_code, provider, cfg):
    """Explain the consent-screen rejections, without guessing at the cause."""
    client_id = cfg[provider]["client_id"] or "(the client id in config.json)"
    org_block = (
        "<br><br>Your organisation controls which apps may read this data. "
        "An administrator can allow it by trusting this OAuth client id:"
        f"<br><code style='color:#2bd4a0;word-break:break-all'>{client_id}</code>"
        "<br><br>Everything the app requests is read-only, and the data never "
        "leaves this machine."
    )
    if error_code in ("admin_policy_enforced", "org_internal"):
        return org_block
    if error_code == "access_denied":
        # Three different things produce this, and only one is the org.
        return (
            "<br><br>This one is ambiguous — it means consent wasn't granted. "
            "In order of likelihood:"
            "<br>1. The consent screen is in <b>Testing</b> and your account "
            "isn't on its <b>Test users</b> list. Add it, then retry."
            "<br>2. Consent was dismissed rather than approved — retry and "
            "choose Allow."
            "<br>3. Your organisation blocks the app outright."
            + org_block
        )
    return ""


def _hint(message):
    low = (message or "").lower()
    for needle, hint in HINTS:
        if needle in low:
            return hint
    return ""


def run_check(cfg):
    """Walk the setup end to end and say precisely what is missing."""
    print("Work Dashboard — setup check\n")

    print("Register these redirect URIs, exactly:")
    for provider in auth.PROVIDERS:
        print(f"  {provider:<11} {redirect_uri(provider)}")

    print(f"\nConfig: {auth.CONFIG_PATH}"
          f"{'' if auth.CONFIG_PATH.exists() else '   (missing — cp config.example.json config.json)'}")
    for provider in auth.PROVIDERS:
        client = cfg[provider]["client_id"]
        secret = "set" if cfg[provider].get("client_secret") else "empty"
        state = f"client_id {client[:24]}… (secret {secret})" if client else "no client_id yet"
        print(f"  [{'x' if client else ' '}] {provider:<11} {state}")
        if provider == "google" and client:
            on = auth.google_features(cfg)
            off = [f for f in auth.ALL_GOOGLE_FEATURES if f not in on]
            print(f"      features on: {', '.join(on) or 'none'}"
                  + (f"   off: {', '.join(off)}" if off else ""))
    if cfg["salesforce"]["client_id"]:
        print(f"      salesforce login_url {cfg['salesforce']['login_url']}")

    print(f"\nTokens: {auth.TOKENS_PATH}")
    for provider in auth.PROVIDERS:
        connected = auth.is_connected(provider)
        note = "connected" if connected else "not connected — click Connect in the app"
        print(f"  [{'x' if connected else ' '}] {provider:<11} {note}")

    print("\nLive calls:")
    failures = 0
    sections = build_overview(cfg)["sections"]
    for name, (label, provider) in FEED_LABELS.items():
        result = sections[name]
        if result["ok"]:
            print(f"  [x] {label:<17} {len(result['items'])} item(s)")
            continue
        if result.get("off"):
            print(f"  [-] {label:<17} switched off in config.json")
            continue
        failures += 1
        # NeedsAuth already says *why* ("revoked", "no client_id") — keep it.
        print(f"  [ ] {label:<17} {result['error'] or 'not connected yet'}")
        hint = _hint(result["error"])
        if hint:
            print(f"      → {hint}")

    live = sum(1 for r in sections.values() if not r.get("off"))
    print(f"\nAll {live} enabled feeds are working." if not failures
          else f"\n{failures} of {live} enabled feeds need attention (see above).")
    return 0 if not failures else 1


# --------------------------------------------------------------------------- #
def main():
    cfg = auth.load_config()
    if CHECK_ONLY:
        raise SystemExit(run_check(cfg))

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
