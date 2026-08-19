#!/usr/bin/env python3
"""
OAuth 2.0 (authorization code + PKCE) for Google Workspace and Salesforce.

Both providers redirect back into the dashboard's own server, so the whole
consent flow happens in the browser tab you already have open. Refresh tokens
are written to tokens.json (chmod 600, gitignored) and nothing ever leaves
this machine except the calls to Google and Salesforce themselves.
"""

import base64
import hashlib
import json
import os
import threading
import time
from pathlib import Path

import httpclient as http

HERE = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("DASHBOARD_CONFIG", HERE / "config.json"))
TOKENS_PATH = Path(os.environ.get("DASHBOARD_TOKENS", HERE / "tokens.json"))

# Read-only scopes: the dashboard shows things, it never sends or changes them.
# Grouped per card, because a managed Workspace may allow some and not others —
# gmail.readonly in particular is a *restricted* scope and is the one most often
# held back. Drop a feature from google.features and its scope is never asked
# for, so the rest of the dashboard can still connect.
GOOGLE_FEATURE_SCOPES = {
    "gmail": ["https://www.googleapis.com/auth/gmail.readonly"],
    "calendar": ["https://www.googleapis.com/auth/calendar.readonly"],
    "chat": ["https://www.googleapis.com/auth/chat.spaces.readonly",
             "https://www.googleapis.com/auth/chat.messages.readonly"],
}
ALL_GOOGLE_FEATURES = list(GOOGLE_FEATURE_SCOPES)
SALESFORCE_SCOPES = ["api", "refresh_token"]

PROVIDERS = ("google", "salesforce")
LABELS = {"google": "Google Workspace", "salesforce": "Salesforce"}

DEFAULT_CONFIG = {
    "google": {"client_id": "", "client_secret": "",
               "features": ["gmail", "calendar", "chat"]},
    "salesforce": {
        "client_id": "",
        "client_secret": "",
        "login_url": "https://login.salesforce.com",
        "api_version": "v61.0",
        "mine_only": False,
    },
    "limits": {"emails": 8, "events": 4, "chats": 8, "cases": 8},
}

# config.json is the normal way in; env vars win so you can run without a file.
ENV_OVERRIDES = {
    ("google", "client_id"): "GOOGLE_CLIENT_ID",
    ("google", "client_secret"): "GOOGLE_CLIENT_SECRET",
    ("salesforce", "client_id"): "SF_CLIENT_ID",
    ("salesforce", "client_secret"): "SF_CLIENT_SECRET",
    ("salesforce", "login_url"): "SF_LOGIN_URL",
    ("salesforce", "api_version"): "SF_API_VERSION",
}

_lock = threading.Lock()


class NeedsAuth(Exception):
    """No usable credentials for a provider — the UI should offer 'Connect'."""

    def __init__(self, provider, message=None):
        self.provider = provider
        super().__init__(message or f"Not connected to {LABELS[provider]} yet.")


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    if CONFIG_PATH.exists():
        try:
            user = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise SystemExit(f"{CONFIG_PATH.name} is not valid JSON: {exc}")
        for section, values in user.items():
            if isinstance(values, dict):
                cfg.setdefault(section, {}).update(values)
            else:
                cfg[section] = values

    for (section, key), env in ENV_OVERRIDES.items():
        if os.environ.get(env):
            cfg[section][key] = os.environ[env]

    cfg["salesforce"]["login_url"] = cfg["salesforce"]["login_url"].rstrip("/")
    return cfg


def is_configured(provider, cfg):
    return bool(cfg[provider]["client_id"])


def google_features(cfg):
    """Which Google cards are switched on, in a stable order."""
    wanted = cfg["google"].get("features") or ALL_GOOGLE_FEATURES
    return [f for f in ALL_GOOGLE_FEATURES if f in wanted]


def google_scopes(cfg):
    scopes = []
    for feature in google_features(cfg):
        scopes.extend(GOOGLE_FEATURE_SCOPES[feature])
    return scopes


# --------------------------------------------------------------------------- #
# Token store
# --------------------------------------------------------------------------- #
def _read_all():
    if not TOKENS_PATH.exists():
        return {}
    try:
        return json.loads(TOKENS_PATH.read_text(encoding="utf-8"))
    except ValueError:
        return {}


def _write_all(data):
    TOKENS_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.chmod(TOKENS_PATH, 0o600)


def stored(provider):
    with _lock:
        return _read_all().get(provider, {})


def remember(provider, payload):
    with _lock:
        data = _read_all()
        merged = data.get(provider, {})
        merged.update({k: v for k, v in payload.items() if v is not None})
        data[provider] = merged
        _write_all(data)
        return merged


def forget(provider):
    with _lock:
        data = _read_all()
        data.pop(provider, None)
        _write_all(data)


def is_connected(provider):
    return bool(stored(provider).get("refresh_token"))


# --------------------------------------------------------------------------- #
# Authorization code flow
# --------------------------------------------------------------------------- #
def new_pkce():
    verifier = base64.urlsafe_b64encode(os.urandom(40)).decode().rstrip("=")
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).decode().rstrip("=")


def _endpoints(provider, cfg):
    if provider == "google":
        return ("https://accounts.google.com/o/oauth2/v2/auth",
                "https://oauth2.googleapis.com/token")
    base = cfg["salesforce"]["login_url"]
    return (f"{base}/services/oauth2/authorize",
            f"{base}/services/oauth2/token")


def authorize_url(provider, cfg, redirect_uri, state, challenge):
    auth_url, _ = _endpoints(provider, cfg)
    scopes = google_scopes(cfg) if provider == "google" else SALESFORCE_SCOPES
    params = {
        "client_id": cfg[provider]["client_id"],
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    if provider == "google":
        # offline + consent is what makes Google hand back a refresh token.
        params.update(access_type="offline", prompt="consent",
                      include_granted_scopes="true")
    import urllib.parse
    return f"{auth_url}?{urllib.parse.urlencode(params)}"


def exchange_code(provider, cfg, code, redirect_uri, verifier):
    _, token_url = _endpoints(provider, cfg)
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": cfg[provider]["client_id"],
        "code_verifier": verifier,
    }
    if cfg[provider].get("client_secret"):
        form["client_secret"] = cfg[provider]["client_secret"]
    payload = http.post(token_url, form=form)
    return remember(provider, _normalize(provider, payload))


def _normalize(provider, payload):
    """Provider token responses → the shape we persist."""
    expires_in = int(payload.get("expires_in") or 3600)
    out = {
        "access_token": payload.get("access_token"),
        "refresh_token": payload.get("refresh_token"),
        "expires_at": time.time() + expires_in - 60,   # refresh a minute early
    }
    if provider == "salesforce":
        out["instance_url"] = payload.get("instance_url")
        # Salesforce's `id` is .../id/<orgId>/<userId> — the cheapest way to
        # learn who we are, for the "my cases only" filter.
        identity = payload.get("id") or ""
        if identity:
            out["user_id"] = identity.rstrip("/").split("/")[-1]
    return out


def credentials(provider, cfg):
    """Return usable credentials, refreshing the access token when stale."""
    if not is_configured(provider, cfg):
        raise NeedsAuth(provider, f"No {provider} client_id in config.json.")

    toks = stored(provider)
    if not toks.get("refresh_token"):
        raise NeedsAuth(provider)
    if toks.get("access_token") and toks.get("expires_at", 0) > time.time():
        return toks

    _, token_url = _endpoints(provider, cfg)
    form = {
        "grant_type": "refresh_token",
        "refresh_token": toks["refresh_token"],
        "client_id": cfg[provider]["client_id"],
    }
    if cfg[provider].get("client_secret"):
        form["client_secret"] = cfg[provider]["client_secret"]
    try:
        payload = http.post(token_url, form=form)
    except http.HttpError as exc:
        if exc.status in (400, 401):
            forget(provider)          # revoked or expired — make the UI re-ask
            raise NeedsAuth(provider, f"{LABELS[provider]} access was revoked — reconnect.") from None
        raise

    fresh = _normalize(provider, payload)
    fresh["refresh_token"] = payload.get("refresh_token") or toks["refresh_token"]
    if provider == "salesforce":
        # A refresh response omits these; keep what we learned at connect time.
        fresh["instance_url"] = fresh.get("instance_url") or toks.get("instance_url")
        fresh["user_id"] = fresh.get("user_id") or toks.get("user_id")
    return remember(provider, fresh)
