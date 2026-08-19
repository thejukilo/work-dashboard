#!/usr/bin/env python3
"""
Parse a Salesforce Lightning "Recent Cases" list into the dashboard's case shape.

Unlike Gmail there is no plain-HTML view — Lightning renders the list client-side
into a <table> of <tr>/<td> once the page settles (the scraper waits for that
before handing us the HTML). Cases are matched by their case-number cell, which
is an 8-digit string linking to /lightning/r/Case/<id>/view; the remaining cells
(subject, status, priority, account) are read positionally from the same row, so
a reordered column layout only mislabels fields rather than dropping the case.

Returns [] rather than guessing when nothing matches; DASHBOARD_SCRAPE_DEBUG=1
dumps the rendered HTML so the selectors can be re-tuned against a real sample.
"""

import re
from html.parser import HTMLParser

_CASE_HREF = re.compile(r"/lightning/r/Case/([0-9A-Za-z]{15,18})/view", re.I)
_CASE_NUMBER = re.compile(r"^\d{6,10}$")
_KNOWN_STATUS = {"new", "working", "escalated", "closed", "in progress",
                 "on hold", "waiting", "open", "waiting on customer"}


class _ListParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows = []
        self._row = None
        self._cell = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = {"text": "", "case_id": None, "title": attrs.get("title", "")}
        elif tag == "a" and self._cell is not None:
            m = _CASE_HREF.search(attrs.get("href", ""))
            if m:
                self._cell["case_id"] = m.group(1)
            if not self._cell["title"]:
                self._cell["title"] = attrs.get("title", "")

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None:
            self._cell["text"] = re.sub(r"\s+", " ", self._cell["text"]).strip()
            self._row.append(self._cell)
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell["text"] += data


def parse_case_list(html, instance_url):
    parser = _ListParser()
    parser.feed(html)
    instance = (instance_url or "").rstrip("/")

    cases = []
    seen = set()
    for row in parser.rows:
        id_cell = next((c for c in row if c["case_id"]), None)
        number_cell = next((c for c in row if _CASE_NUMBER.match(c["text"])), None)
        if not (id_cell or number_cell):
            continue

        case_id = id_cell["case_id"] if id_cell else None
        number = (number_cell or id_cell)["text"]
        key = case_id or number
        if key in seen:
            continue
        seen.add(key)

        texts = [c["text"] for c in row if c["text"]]
        status = next((t for t in texts if t.lower() in _KNOWN_STATUS), "")
        priority = next((t for t in texts if t.lower() in
                         ("high", "medium", "low", "critical")), "")
        # Subject: the longest free-text cell that isn't the number/status/priority.
        skip = {number, status, priority}
        subject = max((t for t in texts if t not in skip and not _CASE_NUMBER.match(t)),
                      key=len, default="")

        cases.append({
            "id": case_id,
            "number": number,
            "subject": subject or "(no subject)",
            "status": status,
            "priority": priority,
            "closed": status.lower() == "closed",
            "account": "",
            "contact": "",
            "owner": "",
            "at": None,
            "url": f"{instance}/lightning/r/Case/{case_id}/view" if case_id and instance
                   else f"{instance}/lightning/o/Case/list",
        })
    return cases
