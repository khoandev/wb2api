#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Probe the web-console conversation create that the daily activity needs.

POST /console/as/conversations/ answered HTTP 401 / code 10034 ("CODEBUDDY_API_KEY
is required but could not be found") for OAuth-only intl accounts from
2026-11-16. The gateway can only guess why, so this script asks the upstream
directly, in two safe steps:

  --read-only   GET the conversation list and the account profile. Proves the
                stored OAuth token still authenticates and that only *create* is
                refused. Nothing is written upstream.
  --probe       POST one create per entry of wb_webcreate.variants(), stopping at
                the first entry that returns a conversation id and stopping and
                deleting every conversation it creates. This one does touch the
                account: run it by hand, on one account, and read the output.

  --dry-run     Print the shapes and the request line without any network call;
                works without an accounts directory.

Examples:
  python3 scripts/probe_web_conversation.py --dry-run
  python3 scripts/probe_web_conversation.py --accounts-dir accounts --uid <prefix> --read-only
  python3 scripts/probe_web_conversation.py --accounts-dir accounts --uid <prefix> --probe --sleep 3
  python3 scripts/probe_web_conversation.py --accounts-dir accounts --probe --json

Credential handling: the tokens live in the account files, and this script never
prints one. Reports carry the uid prefix and header *names* only; the overlay
headers come from wb_webcreate and never contain Authorization / X-Api-Key.

Exit status: 0 ran (whatever the upstream answered), 1 usage/selection error,
2 the request could not be made at all.
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import wb_accounts          # noqa: E402
import wb_identity          # noqa: E402
import wb_webcreate         # noqa: E402


UID_PREFIX_MIN = 6
SECRET_HEADERS = frozenset((
    "authorization", "x-api-key", "x-user-id", "x-device-token",
    "x-access-token", "cookie", "set-cookie",
))


def _read_body(exc):
    try:
        return exc.read().decode("utf-8", "replace")
    except Exception:
        return ""


def _request(account, method, url, body=None, extra_headers=None,
             timeout=30):
    """(status, text) for one request, without raising on 4xx/5xx."""
    headers = dict(account.web_headers())
    headers.update(extra_headers or {})
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with wb_accounts.urlopen(req, timeout=timeout,
                                 proxy=getattr(account, "proxy", "")) as resp:
            return getattr(resp, "status", 200), resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, _read_body(exc)
    except Exception as exc:                      # network / TLS / timeout
        return 0, str(exc)


def _parse(text):
    """Upstream payload -> (code, msg, conversation id); tolerant of junk."""
    try:
        payload = json.loads(text or "{}")
    except Exception:
        return None, (text or "")[:200], ""
    if not isinstance(payload, dict):
        return None, (text or "")[:200], ""
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    conversation = str(data.get("id") or payload.get("id") or "")
    return payload.get("code"), str(payload.get("msg") or payload.get("message") or ""), conversation


def _header_names(headers):
    """Header names only: a diagnostic that prints values leaks credentials."""
    return sorted(name for name in headers or ())


def select_accounts(pool, uid_prefix, realm):
    accounts = [a for a in pool.accounts if not realm or a.realm == realm]
    if uid_prefix:
        if len(uid_prefix) < UID_PREFIX_MIN:
            raise SystemExit("--uid needs at least %d characters" % UID_PREFIX_MIN)
        accounts = [a for a in accounts if str(a.uid or "").startswith(uid_prefix)]
    return accounts


def describe(account):
    """The operator-facing identity of an account, with nothing secret."""
    return {
        "uid": str(account.uid or "")[:8],
        "realm": account.realm,
        "nickname": account.nickname or "",
        "oauth": not account.is_api_key,
        "token_present": bool(account.access_token),
    }


def domain_for(account, override):
    if override:
        return override
    return (account.domain
            or wb_accounts.web_origin_for(account.realm).split("://", 1)[-1])


def ide_version_for(account):
    try:
        return wb_identity.desktop_version(account.realm)
    except Exception:
        return ""


def build_variants(args, account=None):
    prompt = args.prompt
    model = args.model
    domain = domain_for(account, args.domain) if account else (args.domain or "")
    ide_version = ide_version_for(account) if account else (args.ide_version or "")
    body = wb_webcreate.baseline_body(prompt, model)
    return wb_webcreate.variants(body, domain=domain, ide_version=ide_version)


def print_dry_run(variants, args):
    if args.json:
        print(json.dumps({
            "mode": "dry-run",
            "url": wb_accounts.WEB_CONVERSATIONS_URL,
            "variants": [{"label": variant.label,
                          "body_keys": sorted(variant.body),
                          "overlay_headers": _header_names(variant.headers)}
                         for variant in variants],
        }, ensure_ascii=False, indent=2))
        return
    print("dry run: %d shapes, nothing sent to upstream" % len(variants))
    print("url   : POST %s" % wb_accounts.WEB_CONVERSATIONS_URL)
    for index, variant in enumerate(variants, 1):
        overlay = _header_names(variant.headers)
        print("  %d. %-24s body=%-45s headers=%s"
              % (index, variant.label, ",".join(sorted(variant.body)) or "-",
                 ",".join(overlay) or "-"))
    print("next  : --read-only (no write), then --probe (creates conversations)")


def read_only_report(account, args):
    """Conversation list + profile: does the stored token still authenticate?"""
    url = (wb_accounts.WEB_CONVERSATIONS_URL
           + "?page=1&pageSize=1")
    status, text = _request(account, "GET", url, timeout=args.timeout)
    code, msg, _ = _parse(text)
    listing = {"status": status, "code": code, "msg": msg,
               "reachable": 200 <= status < 300}
    origin = wb_accounts.web_origin_for(account.realm)
    status, text = _request(account, "GET", origin + wb_accounts.PROFILE_PATH,
                            timeout=args.timeout)
    code, msg, _ = _parse(text)
    profile = {"status": status, "code": code, "msg": msg,
               "reachable": 200 <= status < 300}
    return {"conversations": listing, "profile": profile}


def probe_create(account, args):
    """Try the wb_webcreate matrix; stop at the first working shape."""
    variants = build_variants(args, account)[:max(1, args.limit)]
    rows = []
    winner = ""
    for index, variant in enumerate(variants, 1):
        if index > 1 and args.sleep > 0:
            time.sleep(args.sleep)
        started = time.time()
        status, text = _request(account, "POST",
                                wb_accounts.WEB_CONVERSATIONS_URL,
                                body=variant.body,
                                extra_headers=variant.headers,
                                timeout=args.timeout)
        code, msg, conversation = _parse(text)
        row = {
            "variant": variant.label,
            "status": status,
            "code": code,
            "msg": msg,
            "conversation": conversation,
            "elapsed_ms": int((time.time() - started) * 1000),
            "overlay_headers": _header_names(variant.headers),
            "cleanup": "",
        }
        if conversation:
            row["cleanup"] = ("released" if account.web_conversation_release(
                conversation, delete=True) else "release failed")
            winner = variant.label
        rows.append(row)
        if not args.json:
            print("  %d. %-24s HTTP %-4s code=%-8s %-40s %s"
                  % (index, variant.label, status or "-", code if code is not None else "-",
                     (msg or "")[:40], row["cleanup"]))
        if winner:
            break
    return {"rows": rows, "winner": winner}


def remaining_conversations(account, args, known):
    """The ids a probe left behind, so a failed cleanup cannot go unnoticed."""
    url = (wb_accounts.WEB_CONVERSATIONS_URL + "?page=1&pageSize=50")
    status, text = _request(account, "GET", url, timeout=args.timeout)
    try:
        payload = json.loads(text or "{}")
        entries = ((payload.get("data") or {}).get("conversations") or [])
    except Exception:
        entries = []
    survivors = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        conversation = str(entry.get("id") or "")
        if conversation and conversation in known:
            survivors.append({"conversation": conversation,
                              "status": str(entry.get("status") or ""),
                              "name": str(entry.get("name") or "")[:40]})
    return {"status": status, "checked": len(entries), "survivors": survivors}


def run(args):
    variants = build_variants(args)
    if args.dry_run:
        print_dry_run(variants, args)
        return 0
    pool = wb_accounts.AccountPool(args.accounts_dir)
    pool.load()
    accounts = select_accounts(pool, args.uid, args.realm)
    if not accounts:
        print("no account matched %s in %s"
              % (args.uid or "(any)", args.accounts_dir), file=sys.stderr)
        return 1
    if len(accounts) > 1 and not args.all:
        print("%d accounts matched; narrow it with --uid <prefix> or pass --all"
              % len(accounts), file=sys.stderr)
        return 1

    report = {"mode": "probe" if args.probe else "read-only",
              "accounts_dir": args.accounts_dir,
              "url": wb_accounts.WEB_CONVERSATIONS_URL,
              "variants": [v.label for v in variants],
              "results": []}
    for account in accounts:
        entry = {"account": describe(account)}
        if not args.json:
            print("account %s (%s, %s)"
                  % (entry["account"]["uid"], entry["account"]["realm"],
                     "oauth" if entry["account"]["oauth"] else "static api key"))
        if args.probe:
            entry["create"] = probe_create(account, args)
            known = [row["conversation"] for row in entry["create"]["rows"]
                     if row["conversation"]]
            entry["leftovers"] = remaining_conversations(account, args, known)
            if not args.json:
                print("  winner: %s" % (entry["create"]["winner"] or "none"))
                if entry["leftovers"]["survivors"]:
                    print("  leftovers: %s" % json.dumps(
                        entry["leftovers"]["survivors"], ensure_ascii=False))
        else:
            entry["read_only"] = read_only_report(account, args)
            if not args.json:
                for name in ("conversations", "profile"):
                    row = entry["read_only"][name]
                    print("  %-14s HTTP %-4s code=%s %s"
                          % (name, row["status"] or "-",
                             row["code"] if row["code"] is not None else "-",
                             (row["msg"] or "")[:60]))
        report["results"].append(entry)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Probe the web-console conversation create used by the "
                    "daily activity (code 10034 / CODEBUDDY_API_KEY).")
    parser.add_argument("--accounts-dir", default=os.path.join(ROOT, "accounts"),
                        help="directory holding the account JSON files")
    parser.add_argument("--uid", default="",
                        help="uid or uid prefix (>= %d chars)" % UID_PREFIX_MIN)
    parser.add_argument("--realm", default="intl", choices=("intl", "cn", ""),
                        help="restrict to one realm (default intl)")
    parser.add_argument("--all", action="store_true",
                        help="run every matched account instead of refusing")
    parser.add_argument("--model", default=wb_accounts.DAILY_CHAT_MODEL,
                        help="model the create body asks for")
    parser.add_argument("--prompt", default=wb_accounts.DAILY_CHAT_WEB_PROMPT,
                        help="prompt the create body carries")
    parser.add_argument("--domain", default="",
                        help="X-Domain for the desktop-headers shape")
    parser.add_argument("--ide-version", default="",
                        help="X-IDE-Version override (default: desktop_version)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--read-only", action="store_true",
                      help="only GET list + profile (default)")
    mode.add_argument("--probe", action="store_true",
                      help="POST one create per shape; stop at the first success")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the shapes, send nothing, need no accounts")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--sleep", type=float, default=2.0,
                        help="seconds between create requests (default 2)")
    parser.add_argument("--limit", type=int, default=len(wb_webcreate.variants(
                            wb_webcreate.baseline_body("", ""))),
                        help="maximum number of shapes to try")
    parser.add_argument("--timeout", type=int, default=30)
    args = parser.parse_args(argv)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
