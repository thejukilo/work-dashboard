#!/usr/bin/env python3
"""
Gmail + Salesforce feeds read through your running Chrome (chrome_cdp), for
managed devices where the API is blocked and only the verified browser gets in.

Same return shapes as sources.py, so the dashboard can't tell the difference.
Parsing lives in gmail_html.py / salesforce_dom.py; this module just points the
CDP reader at the right page and hands the HTML over.
"""

import os

import chrome_cdp
import gmail_html
import salesforce_dom

# Gmail's basic-HTML view: plain server-rendered markup, no JS — stable to parse.
GMAIL_URL = "https://mail.google.com/mail/u/0/h/?ui=html&zy=h"
DEBUG = os.environ.get("DASHBOARD_SCRAPE_DEBUG") == "1"

# Reuse ScrapeError so server.section() handles both browser backends the same.
from scraper import ScrapeError  # noqa: E402


def _dump(name, html):
    if DEBUG and html:
        try:
            open(f"last-{name}.html", "w", encoding="utf-8").write(html)
        except OSError:
            pass


def latest_emails(limit=8):
    try:
        html = chrome_cdp.page_html("mail.google.com", GMAIL_URL)
    except chrome_cdp.CDPError as exc:
        raise ScrapeError(str(exc)) from None
    _dump("gmail", html)
    mails = gmail_html.parse_inbox(html)
    if not mails and "inbox" not in html.lower():
        raise ScrapeError("Read the Gmail tab but found no messages — the markup may "
                          "have changed. Re-run with DASHBOARD_SCRAPE_DEBUG=1 and share "
                          "last-gmail.html.")
    return mails[:limit]


def recent_cases(instance_url, limit=8):
    instance = (instance_url or "").rstrip("/")
    if not instance:
        raise ScrapeError("Set salesforce.instance_url in config.json to your "
                          "Lightning domain, e.g. https://your-domain.my.salesforce.com")
    url = f"{instance}/lightning/o/Case/list?filterName=Recent"
    try:
        html = chrome_cdp.page_html("/lightning/o/Case", url)
    except chrome_cdp.CDPError as exc:
        raise ScrapeError(str(exc)) from None
    _dump("salesforce", html)
    return salesforce_dom.parse_case_list(html, instance)[:limit]
