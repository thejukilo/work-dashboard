#!/usr/bin/env python3
"""
Parse Gmail's "basic HTML" inbox into the dashboard's email shape.

The basic view (mail.google.com/mail/u/0/h/) is a plain <table> of messages with
no JavaScript. Each message row carries a link to the thread (href contains
`th=`), which is the one dependable anchor — so we find rows by that link rather
than by fragile column positions, then read the sender, date and unread state
off the surrounding cells.

If Google reshapes this markup, `parse_inbox` returns [] rather than guessing;
run the scraper with DASHBOARD_SCRAPE_DEBUG=1 to dump the raw HTML and the
selectors can be re-tuned against a real sample.
"""

import re
from html.parser import HTMLParser

_THREAD_HREF = re.compile(r"[?&]th=([0-9a-f]+)", re.I)


class _InboxParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows = []           # list of rows; each row is a list of cells
        self._row = None         # current row's cells
        self._cell = None        # current cell dict
        self._bold_depth = 0
        self._anchor = None      # href of the anchor currently open, if any

    # -- rows / cells ------------------------------------------------------- #
    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "tr":
            self._row = []
        elif tag == "td" and self._row is not None:
            self._cell = {"text": "", "bold": False, "thread": None, "href": None}
        elif tag in ("b", "strong"):
            self._bold_depth += 1
        elif tag == "a":
            href = attrs.get("href", "")
            self._anchor = href
            if self._cell is not None:
                m = _THREAD_HREF.search(href)
                if m:
                    self._cell["thread"] = m.group(1)
                    self._cell["href"] = href

    def handle_endtag(self, tag):
        if tag == "td" and self._cell is not None:
            self._cell["text"] = re.sub(r"\s+", " ", self._cell["text"]).strip()
            self._row.append(self._cell)
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None
        elif tag in ("b", "strong"):
            self._bold_depth = max(0, self._bold_depth - 1)
        elif tag == "a":
            self._anchor = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell["text"] += data
            if self._bold_depth:
                # Unread senders/subjects are bold in the basic view.
                self._cell["bold"] = True


# Cells that are never the sender: the leading checkbox/star gutter.
_GUTTER = re.compile(r"^[\s✓★☆*]*$")
_DATE = re.compile(
    r"""^(
        \d{1,2}:\d{2}\s*(am|pm)?          |   # 3:42 pm
        [A-Z][a-z]{2}\s+\d{1,2}           |   # Aug 19
        \d{1,2}\s+[A-Z][a-z]{2}           |   # 19 Aug
        \d{1,2}/\d{1,2}/\d{2,4}
    )$""",
    re.X | re.I,
)


def parse_inbox(html):
    parser = _InboxParser()
    parser.feed(html)

    mails = []
    seen = set()
    for row in parser.rows:
        subject_cell = next((c for c in row if c["thread"]), None)
        if not subject_cell or subject_cell["thread"] in seen:
            continue
        seen.add(subject_cell["thread"])

        idx = row.index(subject_cell)
        # Sender: the last meaningful cell before the subject.
        sender = ""
        unread = subject_cell["bold"]
        for cell in reversed(row[:idx]):
            if cell["text"] and not _GUTTER.match(cell["text"]):
                sender = cell["text"]
                unread = unread or cell["bold"]
                break
        # Date: the last cell that reads like one.
        when = ""
        for cell in reversed(row[idx + 1:]):
            if _DATE.match(cell["text"]):
                when = cell["text"]
                break

        subject, snippet = _split_subject(subject_cell["text"])
        mails.append({
            "id": subject_cell["thread"],
            "from": sender or "(unknown sender)",
            "address": "",
            "subject": subject or "(no subject)",
            "snippet": snippet,
            "at": None,                  # basic view gives a label, not a timestamp
            "when_label": when,          # shown directly by the UI
            "unread": unread,
            "starred": any("★" in c["text"] for c in row[:idx]),
            "url": _thread_url(subject_cell["href"]),
        })
    return mails


def _split_subject(text):
    """Basic view renders 'Subject - snippet…' in one cell; split on ' - '."""
    if " - " in text:
        subject, snippet = text.split(" - ", 1)
        return subject.strip(), snippet.strip()
    return text.strip(), ""


def _thread_url(href):
    if not href:
        return "https://mail.google.com/mail/u/0/"
    if href.startswith("http"):
        return href
    return "https://mail.google.com/mail/u/0/h/" + href.lstrip("./")
