#!/usr/bin/env python3
"""
The four feeds behind the dashboard.

Every fetcher returns a plain list of dicts with the same handful of keys the
front-end knows how to render, so the UI never has to care that Gmail, Chat and
Salesforce all describe "when" differently.
"""

import email.utils
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import httpclient as http

GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
CALENDAR = "https://www.googleapis.com/calendar/v3"
CHAT = "https://chat.googleapis.com/v1"


def _utc_now():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


# --------------------------------------------------------------------------- #
# Gmail — latest emails
# --------------------------------------------------------------------------- #
def latest_emails(token, limit=8):
    headers = http.bearer(token)
    listing = http.get(f"{GMAIL}/messages",
                       params={"maxResults": limit, "q": "in:inbox"},
                       headers=headers)
    ids = [m["id"] for m in listing.get("messages", [])]
    if not ids:
        return []

    def fetch(msg_id):
        return http.get(
            f"{GMAIL}/messages/{msg_id}",
            params={"format": "metadata",
                    "metadataHeaders": ["From", "Subject", "Date"]},
            headers=headers,
        )

    with ThreadPoolExecutor(max_workers=min(8, len(ids))) as pool:
        messages = list(pool.map(fetch, ids))

    out = []
    for msg in messages:
        sender, address = email.utils.parseaddr(_header(msg, "From"))
        labels = msg.get("labelIds", [])
        out.append({
            "id": msg.get("id"),
            "from": sender or address or "(unknown sender)",
            "address": address,
            "subject": _header(msg, "Subject") or "(no subject)",
            "snippet": _unescape(msg.get("snippet", "")),
            "at": _epoch_ms_to_iso(msg.get("internalDate")),
            "unread": "UNREAD" in labels,
            "starred": "STARRED" in labels,
            "url": f"https://mail.google.com/mail/u/0/#inbox/{msg.get('id')}",
        })
    out.sort(key=lambda m: m["at"] or "", reverse=True)
    return out


def _header(msg, name):
    for header in msg.get("payload", {}).get("headers", []):
        if header.get("name", "").lower() == name.lower():
            return header.get("value", "")
    return ""


def _epoch_ms_to_iso(value):
    if not value:
        return None
    return _iso(datetime.fromtimestamp(int(value) / 1000, timezone.utc))


def _unescape(text):
    return (text.replace("&quot;", '"').replace("&#39;", "'")
                .replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">"))


# --------------------------------------------------------------------------- #
# Calendar — next appointment (plus what follows it)
# --------------------------------------------------------------------------- #
def upcoming_events(token, limit=4):
    now = _utc_now()
    data = http.get(
        f"{CALENDAR}/calendars/primary/events",
        params={
            "timeMin": _iso(now - timedelta(minutes=30)),  # keep a meeting you're in
            "timeMax": _iso(now + timedelta(days=14)),
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": limit * 4,                       # room to drop declines
        },
        headers=http.bearer(token),
    )

    events = []
    for item in data.get("items", []):
        if item.get("status") == "cancelled" or _declined(item):
            continue
        start, all_day = _when(item.get("start", {}))
        end, _ = _when(item.get("end", {}))
        if not start or (end and end < now):
            continue
        attendees = [a for a in item.get("attendees", []) if not a.get("resource")]
        events.append({
            "id": item.get("id"),
            "title": item.get("summary") or "(no title)",
            "start": _iso(start),
            "end": _iso(end) if end else None,
            "all_day": all_day,
            "location": item.get("location") or "",
            "meet_url": item.get("hangoutLink") or _conference_url(item),
            "organizer": (item.get("organizer") or {}).get("displayName")
                         or (item.get("organizer") or {}).get("email", ""),
            "attendees": len(attendees),
            "accepted": _my_response(item) == "accepted",
            "url": item.get("htmlLink"),
        })
        if len(events) >= limit:
            break
    return events


def _when(slot):
    """Calendar gives either dateTime (timed) or date (all-day)."""
    if slot.get("dateTime"):
        return datetime.fromisoformat(slot["dateTime"]), False
    if slot.get("date"):
        naive = datetime.fromisoformat(slot["date"])
        return naive.replace(tzinfo=timezone.utc), True
    return None, False


def _my_response(item):
    for attendee in item.get("attendees", []):
        if attendee.get("self"):
            return attendee.get("responseStatus")
    return None


def _declined(item):
    return _my_response(item) == "declined"


def _conference_url(item):
    for entry in (item.get("conferenceData", {}).get("entryPoints") or []):
        if entry.get("entryPointType") == "video" and entry.get("uri"):
            return entry["uri"]
    return ""


# --------------------------------------------------------------------------- #
# Google Chat — latest messages across your spaces
# --------------------------------------------------------------------------- #
def latest_chats(token, limit=8):
    headers = http.bearer(token)
    spaces = http.get(f"{CHAT}/spaces", params={"pageSize": 100},
                      headers=headers).get("spaces", [])
    if not spaces:
        return []

    def recent(space):
        try:
            data = http.get(f"{CHAT}/{space['name']}/messages",
                            params={"pageSize": 3, "orderBy": "createTime desc"},
                            headers=headers)
        except http.HttpError:
            return []                       # a space we can't read shouldn't kill the feed
        return [_chat_message(space, msg) for msg in data.get("messages", [])]

    with ThreadPoolExecutor(max_workers=min(8, len(spaces))) as pool:
        batches = list(pool.map(recent, spaces))

    messages = [m for batch in batches for m in batch if m["text"]]
    messages.sort(key=lambda m: m["at"] or "", reverse=True)
    return messages[:limit]


def _chat_message(space, msg):
    sender = msg.get("sender", {}) or {}
    who = sender.get("displayName") or "Someone"
    space_id = space["name"].split("/")[-1]
    is_dm = space.get("spaceType") == "DIRECT_MESSAGE" or space.get("singleUserBotDm")
    return {
        "id": msg.get("name"),
        "from": who,
        "space": space.get("displayName") or (who if is_dm else "Group chat"),
        "direct": bool(is_dm),
        "text": (msg.get("text") or "").strip(),
        "at": msg.get("createTime"),
        "url": f"https://mail.google.com/chat/u/0/#chat/space/{space_id}",
    }


# --------------------------------------------------------------------------- #
# Salesforce — last cases
# --------------------------------------------------------------------------- #
CASE_FIELDS = ("Id, CaseNumber, Subject, Status, Priority, IsClosed, "
               "LastModifiedDate, CreatedDate, Account.Name, Contact.Name, Owner.Name")


def recent_cases(creds, cfg, limit=8):
    instance = (creds.get("instance_url") or "").rstrip("/")
    if not instance:
        raise http.HttpError(0, "salesforce", "no instance_url on the stored token")

    where = ""
    if cfg["salesforce"].get("mine_only") and creds.get("user_id"):
        where = f"WHERE OwnerId = '{_soql_safe(creds['user_id'])}' "
    soql = (f"SELECT {CASE_FIELDS} FROM Case {where}"
            f"ORDER BY LastModifiedDate DESC LIMIT {int(limit)}")

    version = cfg["salesforce"].get("api_version", "v61.0")
    data = http.get(f"{instance}/services/data/{version}/query",
                    params={"q": soql}, headers=http.bearer(creds["access_token"]))

    cases = []
    for row in data.get("records", []):
        cases.append({
            "id": row.get("Id"),
            "number": row.get("CaseNumber"),
            "subject": row.get("Subject") or "(no subject)",
            "status": row.get("Status") or "",
            "priority": row.get("Priority") or "",
            "closed": bool(row.get("IsClosed")),
            "account": (row.get("Account") or {}).get("Name", ""),
            "contact": (row.get("Contact") or {}).get("Name", ""),
            "owner": (row.get("Owner") or {}).get("Name", ""),
            "at": row.get("LastModifiedDate"),
            "url": f"{instance}/lightning/r/Case/{row.get('Id')}/view",
        })
    return cases


def _soql_safe(value):
    """Salesforce ids are alphanumeric; strip anything that isn't."""
    return re.sub(r"[^A-Za-z0-9]", "", value or "")
