# -*- coding: utf-8 -*-
"""Request shapes for the web-console "create conversation" call.

The intl daily activity drives a real web conversation through
POST /console/as/conversations/ (wb_accounts.Account._web_conversation_create).
On 2026-11-16 that call started answering HTTP 401 / code 10034
("CODEBUDDY_API_KEY is required but could not be found") for every OAuth-only
account. The answer does not say which part of the request the upstream rejects,
and the captured body behind #90 cannot be re-captured without an interactive
login, so the shape is probed instead of assumed.

``baseline_body`` is exactly what the gateway has been sending since #90: an
install whose create still succeeds must not see a different request because of
this probing. Every other entry of ``variants`` is an overlay that is only
tried after the baseline was refused with the 10034 gate — first by the manual
probe (one request per shape, under a human's eyes), and only if that finds a
working shape, by the gateway itself.

Deliberately dependency-free (plain dicts in, plain dicts out, no wb_* imports):
the probe has to run against an installed tree, and the suites can pin the
matrix without building an Account.
"""

import collections

#: The two body fields the #90 capture pinned: the web app always sends both.
CONVERSATION_ORIGIN = "workbuddy-app"
CONVERSATION_PLUGINS = (("weixinpay", "codebuddy-builtin"),)

#: Non-credential markers of the desktop agent profile (wb_identity,
#: PRODUCT_DESKTOP). Kept to a tuple of pairs so the overlay stays a constant and
#: can never carry an Authorization / X-Api-Key / device-token value.
DESKTOP_AGENT_HEADERS = (
    ("X-Agent-Intent", "craft"),
    ("X-Agent-Purpose", "conversation"),
    ("X-Agent-Type", "main"),
    ("X-IDE-Name", "WorkBuddy"),
    ("X-IDE-Type", "WorkBuddy"),
    ("X-Private-Data", "true"),
    ("X-Product", "SaaS"),
)

#: One probe entry: the request body, plus headers overlaying the account's own
#: web headers (auth stays the caller's business, see DESKTOP_AGENT_HEADERS).
Variant = collections.namedtuple("Variant", "label body headers")


def baseline_body(prompt, model):
    """The create body the gateway sends today; probes must not drift from it.

    A fresh dict every call: the plugins list is mutable, and a caller that
    edits one body must not edit the next request's.
    """
    return {
        "prompt": prompt,
        "model": model,
        "conversationOrigin": CONVERSATION_ORIGIN,
        "plugins": [{"name": name, "marketplace": marketplace}
                    for name, marketplace in CONVERSATION_PLUGINS],
    }


def desktop_headers(domain="", ide_version=""):
    """Desktop agent markers, for the "does the browser shape matter?" probe."""
    headers = dict(DESKTOP_AGENT_HEADERS)
    if ide_version:
        headers["X-IDE-Version"] = str(ide_version)
    if domain:
        headers["X-Domain"] = str(domain)
    return headers


def _copy(body):
    """Shallow copy, with the plugins list rebuilt so entries stay independent."""
    copied = dict(body)
    if "plugins" in copied:
        copied["plugins"] = [dict(plugin) for plugin in (copied["plugins"] or [])
                             if isinstance(plugin, dict)]
    return copied


def _without(body, *keys):
    trimmed = _copy(body)
    for key in keys:
        trimmed.pop(key, None)
    return trimmed


def _reorigin(body, origin):
    changed = _copy(body)
    changed["conversationOrigin"] = origin
    return changed


def variants(body, domain="", ide_version=""):
    """Ordered probe matrix for one baseline body; entry 0 is that body itself.

    ``body`` is what ``baseline_body`` returned for this turn. Every entry gets
    its own copy, so a probe may log or edit one variant without touching the
    next. Ordered by how far the shape moves from the baseline, so a probe that
    stops at the first working entry has created as few conversations as it
    could. ``domain`` / ``ide_version`` only feed the desktop-headers overlay;
    they are passed in rather than imported so this module stays testable
    without an Account.
    """
    base = _copy(body)
    return [
        Variant("baseline", base, {}),
        Variant("no-plugins", _without(base, "plugins"), {}),
        Variant("no-model", _without(base, "model"), {}),
        Variant("origin-workbuddy-web", _reorigin(base, "workbuddy-web"), {}),
        Variant("origin-codebuddy-web", _reorigin(base, "codebuddy-web"), {}),
        Variant("desktop-headers", base, desktop_headers(domain, ide_version)),
        Variant("no-model-no-plugins", _without(base, "model", "plugins"), {}),
    ]
