#!/usr/bin/env python3
"""
Browser-scrape source: read Gmail and Salesforce out of a real, logged-in
browser instead of the APIs.

Why this exists: some Google Workspace orgs (Roche among them) only allow
*approved* OAuth apps to read mail, so the API path is blocked. This path
sidesteps OAuth entirely — you log into a normal Chrome window yourself, once,
exactly as you would by hand (SSO, MFA and all). Playwright keeps that logged-in
session in a profile directory on your machine, and later opens the same profile
headless to read the page you're already allowed to see.

    python3 scraper.py login gmail        # opens Chrome, you sign in, press Enter
    python3 scraper.py login salesforce
    python3 scraper.py test  gmail        # print what it can currently extract

Nothing is embedded in the dashboard (Google and Salesforce both forbid being
iframed); the browser runs as its own window. Session data lives under
browser-profiles/ (gitignored) and never leaves the machine.

Requires Playwright:  pip install playwright  &&  playwright install chromium
(If you have Chrome installed, it's used by default — Google is far less likely
to flag your real browser than a bundled one.)
"""

import os
import sys
from pathlib import Path

import gmail_html
import salesforce_dom

HERE = Path(__file__).resolve().parent
PROFILE_ROOT = Path(os.environ.get("DASHBOARD_PROFILES", HERE / "browser-profiles"))
# Real Chrome by default; set DASHBOARD_BROWSER_CHANNEL="" to use bundled Chromium.
CHANNEL = os.environ.get("DASHBOARD_BROWSER_CHANNEL", "chrome")
DEBUG = os.environ.get("DASHBOARD_SCRAPE_DEBUG") == "1"

# The one page each scraper reads. Gmail's "basic HTML" view is plain server-
# rendered markup with no JavaScript — far more stable to parse than the normal
# JS app, and it loads the same session.
GMAIL_INBOX = "https://mail.google.com/mail/u/0/h/?ui=html&zy=h"
GMAIL_LOGIN = "https://mail.google.com/mail/u/0/h/"


class ScrapeError(Exception):
    """Something went wrong reading the page (not logged in, markup changed…)."""


def _require_playwright():
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
        return sync_playwright
    except ImportError:
        raise ScrapeError(
            "Playwright isn't installed. Run:  pip install playwright && "
            "playwright install chromium") from None


def _profile_dir(provider):
    path = PROFILE_ROOT / provider
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


def _launch(sync_playwright, provider, headless):
    pw = sync_playwright().start()
    kwargs = {"headless": headless, "args": ["--no-sandbox"]}
    if CHANNEL:
        kwargs["channel"] = CHANNEL
    try:
        ctx = pw.chromium.launch_persistent_context(_profile_dir(provider), **kwargs)
    except Exception as exc:
        pw.stop()
        # Most commonly: channel="chrome" requested but Chrome isn't installed.
        raise ScrapeError(
            f"Could not launch the browser ({exc}). If Chrome isn't installed, "
            "set DASHBOARD_BROWSER_CHANNEL= to use the bundled Chromium.") from None
    return pw, ctx


# --------------------------------------------------------------------------- #
# Interactive login (headed) — run once per provider, re-run when it expires
# --------------------------------------------------------------------------- #
def login(provider, start_url):
    sync_playwright = _require_playwright()
    pw, ctx = _launch(sync_playwright, provider, headless=False)
    try:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(start_url, wait_until="domcontentloaded")
        print(f"\nA Chrome window opened on {provider}.")
        print("Log in there as you normally would (SSO, MFA, the lot).")
        input("When you can see your inbox / Salesforce, come back here and press Enter… ")
    finally:
        ctx.close()
        pw.stop()
    print(f"Saved the {provider} session to {PROFILE_ROOT / provider}")


# --------------------------------------------------------------------------- #
# Headless reads used by the dashboard
# --------------------------------------------------------------------------- #
def _fetch_html(provider, url):
    sync_playwright = _require_playwright()
    pw, ctx = _launch(sync_playwright, provider, headless=True)
    try:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(500)
        html = page.content()
        final = page.url
    finally:
        ctx.close()
        pw.stop()

    if DEBUG:
        (PROFILE_ROOT / f"last-{provider}.html").write_text(html, encoding="utf-8")
    if _looks_like_login(final):
        raise ScrapeError(
            f"Not logged in ({provider} redirected to a sign-in page). "
            f"Run:  python3 scraper.py login {provider}")
    return html, final


def _looks_like_login(url):
    low = (url or "").lower()
    return any(s in low for s in ("accounts.google.com", "signin", "login.salesforce",
                                  "/login", "authenticationerror"))


def latest_emails(limit=8):
    html, _ = _fetch_html("gmail", GMAIL_INBOX)
    mails = gmail_html.parse_inbox(html)
    if not mails and "inbox" not in html.lower():
        raise ScrapeError("Read Gmail but found no messages — the markup may have "
                          "changed. Re-run with DASHBOARD_SCRAPE_DEBUG=1 and share "
                          "browser-profiles/last-gmail.html")
    return mails[:limit]


def recent_cases(instance_url, limit=8):
    instance = (instance_url or "").rstrip("/")
    if not instance:
        raise ScrapeError("Set salesforce.instance_url in config.json to your "
                          "Lightning domain, e.g. https://roche.my.salesforce.com")
    url = f"{instance}/lightning/o/Case/list?filterName=Recent"
    html, _ = _fetch_html("salesforce", url)
    return salesforce_dom.parse_case_list(html, instance)[:limit]


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
_LOGIN_URLS = {"gmail": GMAIL_LOGIN, "salesforce": None}


def _main(argv):
    if len(argv) < 2 or argv[1] not in ("login", "test"):
        print(__doc__)
        return 2
    cmd = argv[1]
    provider = argv[2] if len(argv) > 2 else ""

    try:
        if cmd == "login":
            if provider == "salesforce":
                instance = os.environ.get("SF_INSTANCE_URL", "https://login.salesforce.com")
                login("salesforce", instance)
            elif provider == "gmail":
                login("gmail", GMAIL_LOGIN)
            else:
                print("Usage: python3 scraper.py login gmail|salesforce")
                return 2
        else:  # test
            if provider == "gmail":
                for m in latest_emails():
                    print(f"  {m['at'] or '':<20} {m['from'][:24]:<24} {m['subject']}")
            elif provider == "salesforce":
                inst = os.environ.get("SF_INSTANCE_URL", "")
                for c in recent_cases(inst):
                    print(f"  #{c['number']:<10} {c['status'][:14]:<14} {c['subject']}")
            else:
                print("Usage: python3 scraper.py test gmail|salesforce")
                return 2
    except ScrapeError as exc:
        print(f"\n{exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
