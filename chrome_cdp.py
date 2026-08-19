#!/usr/bin/env python3
"""
Talk to a running Chrome over the DevTools Protocol — no Playwright, no pip.

Why this and not Playwright: on a managed device, Google's Context-Aware Access
only lets *your real, verified* Chrome reach the data (right profile, EndPoint
Verification synced). A browser Playwright launches is a different, unverified
one, so it's refused. Driving the Chrome you're already logged into means the
requests come from the browser that passes the check.

It needs nothing installed — only Python's standard library, so it runs under a
locked-down policy that blocks pip and unapproved executables. You start Chrome
with a debug port; this connects to it, reads the DOM of a page you're logged
into, and hands the HTML to the same parsers the API path never needed.

    Start Chrome with the debug port (see README for the exact command), then:
        python3 chrome_cdp.py targets           # list open tabs
        python3 chrome_cdp.py html <url-substr>  # dump one tab's rendered HTML
"""

import base64
import hashlib
import json
import os
import socket
import struct
import sys
import time
import urllib.request
from urllib.parse import urlparse, quote

PORT = int(os.environ.get("DASHBOARD_CHROME_PORT", "9222"))
HOST = os.environ.get("DASHBOARD_CHROME_HOST", "127.0.0.1")


class CDPError(Exception):
    """Chrome wasn't reachable, or a command failed."""


# --------------------------------------------------------------------------- #
# DevTools HTTP endpoints (target discovery)
# --------------------------------------------------------------------------- #
def _http(method, path):
    url = f"http://{HOST}:{PORT}{path}"
    req = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            body = resp.read().decode("utf-8", "replace")
    except Exception as exc:
        raise CDPError(
            f"Couldn't reach Chrome's debug port at {HOST}:{PORT} ({exc}). "
            "Start Chrome with --remote-debugging-port=9222 (see README)."
        ) from None
    return json.loads(body) if body.strip().startswith(("{", "[")) else body


def targets():
    """Every open tab/page, as {title, url, webSocketDebuggerUrl, …}."""
    return [t for t in _http("GET", "/json") if t.get("type") == "page"]


def open_tab(url):
    """Ask Chrome to open a new tab (PUT for current Chrome, GET as fallback)."""
    path = f"/json/new?{quote(url, safe='')}"
    try:
        return _http("PUT", path)
    except CDPError:
        return _http("GET", path)


def find_or_open(url_substring, open_url):
    for target in targets():
        if url_substring in target.get("url", ""):
            return target
    tab = open_tab(open_url)
    time.sleep(2.0)   # let it start loading before we attach
    return tab


# --------------------------------------------------------------------------- #
# Minimal WebSocket client (RFC 6455, client side only)
# --------------------------------------------------------------------------- #
class _WS:
    def __init__(self, ws_url):
        parts = urlparse(ws_url)
        self.sock = socket.create_connection((parts.hostname, parts.port or PORT), timeout=15)
        self.sock.settimeout(20)
        self._handshake(parts)
        self._buf = b""

    def _handshake(self, parts):
        key = base64.b64encode(os.urandom(16)).decode()
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {parts.hostname}:{parts.port}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(req.encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise CDPError("Chrome closed the DevTools socket during handshake.")
            resp += chunk
        if b" 101 " not in resp.split(b"\r\n", 1)[0]:
            raise CDPError("Chrome refused the WebSocket upgrade (unexpected response).")
        accept = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
        ).decode()
        if accept.encode() not in resp:
            raise CDPError("DevTools handshake failed its Sec-WebSocket-Accept check.")

    # -- framing -------------------------------------------------------------
    def send(self, text):
        payload = text.encode("utf-8")
        header = bytearray([0x81])                 # FIN + text opcode
        mask = os.urandom(4)
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def _read(self, n):
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise CDPError("Chrome closed the DevTools socket.")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def recv(self):
        """Return one full application message (reassembles fragments)."""
        data = b""
        while True:
            b0, b1 = self._read(2)
            fin = b0 & 0x80
            opcode = b0 & 0x0F
            length = b1 & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._read(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._read(8))[0]
            payload = self._read(length) if length else b""
            if opcode == 0x8:                        # close
                raise CDPError("Chrome closed the DevTools connection.")
            if opcode == 0x9:                        # ping → pong, keep reading
                continue
            if opcode == 0xA:                        # pong
                continue
            data += payload
            if fin:
                return data.decode("utf-8", "replace")

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# CDP command helpers
# --------------------------------------------------------------------------- #
class Session:
    def __init__(self, ws_url):
        self.ws = _WS(ws_url)
        self._id = 0

    def call(self, method, params=None, timeout=20):
        self._id += 1
        want = self._id
        self.ws.send(json.dumps({"id": want, "method": method, "params": params or {}}))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == want:                # ignore protocol events
                if "error" in msg:
                    raise CDPError(f"{method} failed: {msg['error'].get('message')}")
                return msg.get("result", {})
        raise CDPError(f"{method} timed out after {timeout}s")

    def eval_js(self, expression):
        result = self.call("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": True,
        })
        return result.get("result", {}).get("value")

    def close(self):
        self.ws.close()


def page_html(url_substring, open_url, settle=1.5):
    """Attach to (or open) a tab, wait for it to render, return its HTML."""
    target = find_or_open(url_substring, open_url)
    ws_url = target.get("webSocketDebuggerUrl")
    if not ws_url:
        raise CDPError("That tab exposes no debugger URL — is this the right Chrome?")

    session = Session(ws_url)
    try:
        # If we opened a fresh tab it may still be loading; nudge and settle.
        session.eval_js(f"location.href.includes({json.dumps(url_substring)}) "
                        f"|| (location.href = {json.dumps(open_url)})")
        time.sleep(settle)
        for _ in range(20):                          # up to ~10s for readyState
            if session.eval_js("document.readyState") == "complete":
                break
            time.sleep(0.5)
        html = session.eval_js("document.documentElement.outerHTML")
        final = session.eval_js("location.href")
    finally:
        session.close()

    if _looks_like_login(final):
        raise CDPError(
            f"That tab is on a sign-in / access page ({final[:80]}). Log into "
            "this Chrome first, then retry.")
    return html or ""


def _looks_like_login(url):
    low = (url or "").lower()
    return any(s in low for s in ("accounts.google.com", "signin", "login.salesforce",
                                  "/login", "googlecaa", "denied"))


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    try:
        if argv[1] == "targets":
            for t in targets():
                print(f"  {t.get('url','')[:90]}")
        elif argv[1] == "html" and len(argv) > 2:
            print(page_html(argv[2], argv[2] if argv[2].startswith("http") else "about:blank"))
        else:
            print("Usage: python3 chrome_cdp.py targets | html <url-substring>")
            return 2
    except CDPError as exc:
        print(f"\n{exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
