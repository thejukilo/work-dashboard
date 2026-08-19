#!/usr/bin/env python3
"""Minimal JSON-over-HTTPS client built on urllib — no third-party deps."""

import json
import ssl
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT = 25
_SSL = ssl.create_default_context()
UA = "work-dashboard/1.0"


class HttpError(Exception):
    """A non-2xx response (status 0 means the host was unreachable)."""

    def __init__(self, status, url, body):
        self.status = status
        self.url = url
        self.body = body
        super().__init__(f"HTTP {status} — {describe(body)}")


def describe(body, limit=300):
    """Best-effort human message out of whatever the provider sent back."""
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):                      # Google style
            return str(err.get("message") or err)
        if isinstance(err, str):                       # OAuth style
            return f"{err}: {body.get('error_description', '')}".strip(": ")
        if isinstance(body.get("message"), str):       # Salesforce style
            return body["message"]
    if isinstance(body, list) and body and isinstance(body[0], dict):
        return str(body[0].get("message") or body[0])  # Salesforce error array
    text = body if isinstance(body, str) else json.dumps(body)
    return text[:limit]


def request(method, url, *, params=None, form=None, json_body=None,
            headers=None, timeout=TIMEOUT):
    """Send a request and return its decoded JSON body ({} when empty)."""
    if params:
        url = f"{url}?{urllib.parse.urlencode(params, doseq=True)}"

    hdrs = {"Accept": "application/json", "User-Agent": UA}
    data = None
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        hdrs["Content-Type"] = "application/x-www-form-urlencoded"
    elif json_body is not None:
        data = json.dumps(json_body).encode()
        hdrs["Content-Type"] = "application/json"
    hdrs.update(headers or {})

    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, context=_SSL, timeout=timeout) as resp:
            return _decode(resp.read())
    except urllib.error.HTTPError as exc:
        raise HttpError(exc.code, url, _decode(exc.read())) from None
    except urllib.error.URLError as exc:
        raise HttpError(0, url, f"could not reach {urllib.parse.urlparse(url).netloc}: {exc.reason}") from None


def _decode(raw):
    if not raw:
        return {}
    text = raw.decode("utf-8", "replace")
    try:
        return json.loads(text)
    except ValueError:
        return text


def get(url, **kw):
    return request("GET", url, **kw)


def post(url, **kw):
    return request("POST", url, **kw)


def bearer(token):
    return {"Authorization": f"Bearer {token}"}
