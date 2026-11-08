"""Extract values from the Keycloak login pages.

The pages embed a JavaScript object literal ``const kcContext = {...}`` that is not valid JSON
(it contains functions), so we pull out single values with targeted regexes.
"""
from __future__ import annotations

import json
import re

_PAGE_ID = re.compile(r'"pageId"\s*:\s*"([^"]+)"')
_LOGIN_ACTION = re.compile(r'"loginAction"\s*:\s*"([^"]+)"')
_MESSAGE = re.compile(r'"message"\s*:\s*\{\s*"summary"\s*:\s*"((?:[^"\\]|\\.)*)"\s*,\s*"type"\s*:\s*"(\w+)"')


def _unescape(raw: str) -> str:
    try:
        return json.loads(f'"{raw}"')
    except ValueError:
        return raw.replace("\\/", "/")


def page_id(html: str) -> str | None:
    """'login' (password form) or 'login-sms' (code form); None if not a login page."""
    m = _PAGE_ID.search(html)
    return m.group(1) if m else None


def login_action(html: str) -> str | None:
    """URL the form of the current step must be POSTed to (contains a fresh session_code)."""
    m = _LOGIN_ACTION.search(html)
    return _unescape(m.group(1)) if m else None


def message(html: str) -> tuple[str, str] | None:
    """(type, summary) of the server message, e.g. ('error', '...'). The text is localized."""
    m = _MESSAGE.search(html)
    return (m.group(2), _unescape(m.group(1))) if m else None


def without_session_code(url: str) -> str:
    """The 'send new code' URL: the loginAction without its session_code parameter."""
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "session_code"]
    return urlunsplit(parts._replace(query=urlencode(query)))
