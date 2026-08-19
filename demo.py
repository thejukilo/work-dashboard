#!/usr/bin/env python3
"""
Sample data for `?demo` — the whole UI, none of the accounts.

Timestamps are generated relative to right now, so the relative-time labels
("12m ago", "in 25 min") stay believable whenever you open it.
"""

from datetime import datetime, timedelta, timezone


def _at(**delta):
    stamp = datetime.now(timezone.utc) + timedelta(**delta)
    return stamp.isoformat().replace("+00:00", "Z")


def overview():
    return {
        "demo": True,
        "generated_at": _at(),
        "sections": {
            "emails": _ok([
                {"id": "d1", "from": "Marijke de Vries", "address": "marijke@example.com",
                 "subject": "Re: Q3 renewal — pricing sheet attached",
                 "snippet": "Thanks for the quick turnaround. One open point on the volume tier before legal signs off…",
                 "at": _at(minutes=-12), "unread": True, "starred": False, "url": "#"},
                {"id": "d2", "from": "Salesforce Notifications", "address": "noreply@salesforce.com",
                 "subject": "Case 00104512 was escalated",
                 "snippet": "Priority raised to High by Tom Baker. SLA clock: 4h remaining.",
                 "at": _at(minutes=-48), "unread": True, "starred": False, "url": "#"},
                {"id": "d3", "from": "Anna Kowalski", "address": "anna@example.com",
                 "subject": "Deck for Thursday's steering committee",
                 "snippet": "Draft is in the shared drive — slides 6 and 7 still need your numbers.",
                 "at": _at(hours=-2, minutes=-10), "unread": False, "starred": True, "url": "#"},
                {"id": "d4", "from": "IT Service Desk", "address": "servicedesk@example.com",
                 "subject": "Your VPN certificate expires in 7 days",
                 "snippet": "Renew from the self-service portal — takes about two minutes.",
                 "at": _at(hours=-5), "unread": False, "starred": False, "url": "#"},
                {"id": "d5", "from": "Pieter Janssen", "address": "pieter@example.com",
                 "subject": "Lunch Thursday?",
                 "snippet": "I'm in the Utrecht office all day, would be good to catch up.",
                 "at": _at(hours=-7, minutes=-30), "unread": False, "starred": False, "url": "#"},
            ]),
            "events": _ok([
                {"id": "e1", "title": "Weekly pipeline review", "start": _at(minutes=25),
                 "end": _at(minutes=55), "all_day": False, "location": "",
                 "meet_url": "https://meet.google.com/abc-defg-hij",
                 "organizer": "Anna Kowalski", "attendees": 6, "accepted": True, "url": "#"},
                {"id": "e2", "title": "1:1 with Tom", "start": _at(hours=2),
                 "end": _at(hours=2, minutes=30), "all_day": False, "location": "Room 4.12",
                 "meet_url": "", "organizer": "Tom Baker", "attendees": 2,
                 "accepted": True, "url": "#"},
                {"id": "e3", "title": "Customer workshop — Acme", "start": _at(days=1, hours=1),
                 "end": _at(days=1, hours=4), "all_day": False, "location": "Amsterdam HQ",
                 "meet_url": "", "organizer": "You", "attendees": 9,
                 "accepted": True, "url": "#"},
                {"id": "e4", "title": "Quarterly business review", "start": _at(days=2, hours=3),
                 "end": _at(days=2, hours=5), "all_day": False, "location": "",
                 "meet_url": "https://meet.google.com/xyz-1234-abc",
                 "organizer": "Marijke de Vries", "attendees": 12, "accepted": False, "url": "#"},
            ]),
            "cases": _ok([
                {"id": "c1", "number": "00104512", "subject": "Instrument reports calibration drift after firmware update",
                 "status": "Escalated", "priority": "High", "closed": False,
                 "account": "Acme Diagnostics", "contact": "Tom Baker", "owner": "You",
                 "at": _at(minutes=-35), "url": "#"},
                {"id": "c2", "number": "00104498", "subject": "Reagent lot mismatch in batch import",
                 "status": "In Progress", "priority": "Medium", "closed": False,
                 "account": "Northwind Labs", "contact": "Sofia Rossi", "owner": "You",
                 "at": _at(hours=-3), "url": "#"},
                {"id": "c3", "number": "00104471", "subject": "Request: additional user licences for site B",
                 "status": "Waiting on Customer", "priority": "Low", "closed": False,
                 "account": "Globex Health", "contact": "Daniel Weiss", "owner": "Anna Kowalski",
                 "at": _at(hours=-9), "url": "#"},
                {"id": "c4", "number": "00104455", "subject": "Sample tracking export missing timestamps",
                 "status": "Closed", "priority": "Medium", "closed": True,
                 "account": "Acme Diagnostics", "contact": "Tom Baker", "owner": "You",
                 "at": _at(days=-1, hours=-2), "url": "#"},
            ]),
            "chats": _ok([
                {"id": "m1", "from": "Tom Baker", "space": "Tom Baker", "direct": True,
                 "text": "Did the firmware note land with the customer? They pinged me again.",
                 "at": _at(minutes=-4), "url": "#"},
                {"id": "m2", "from": "Anna Kowalski", "space": "Pipeline — EMEA", "direct": False,
                 "text": "Pushed the updated forecast, numbers look better than last week.",
                 "at": _at(minutes=-21), "url": "#"},
                {"id": "m3", "from": "Sofia Rossi", "space": "Support escalations", "direct": False,
                 "text": "00104512 needs an owner decision before 16:00.",
                 "at": _at(minutes=-58), "url": "#"},
                {"id": "m4", "from": "Pieter Janssen", "space": "Pieter Janssen", "direct": True,
                 "text": "Lunch Thursday works for me 👍",
                 "at": _at(hours=-3, minutes=-12), "url": "#"},
            ]),
        },
    }


def _ok(items):
    return {"ok": True, "items": items, "error": None, "needs_auth": False}


def status():
    return {
        "demo": True,
        "providers": {
            "google": {"configured": True, "connected": True},
            "salesforce": {"configured": True, "connected": True},
        },
    }
