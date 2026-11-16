# -*- coding: utf-8 -*-
"""The 10034 create gate: one attempt, an honest message, no account damage.

    python tests/_test_daily_chat_web_gate.py

Background: from 2026-11-16 the intl daily activity's web leg answered
HTTP 401 / code 10034 ("CODEBUDDY_API_KEY is required but could not be found")
for OAuth-only accounts. Three things have to stay true about how the gateway
treats that answer:

  1. **One attempt.** The same request cannot start working by being sent again,
     so it must not burn the retry budget, fall through every candidate model, or
     pay the create-failure backoff.
  2. **An honest message.** It is an upstream account-type gate, not a broken
     credential: the operator must not be told to retry or to log in again, and
     must not read a bare "HTTP 401" as the cause.
  3. **No side effects.** No ban flag, no "last daily chat" stamp, no token
     refresh, no activity report unless the operator turned the fallback on.

A plain 401 (stale token, code 11140 ban) must keep its old meaning — this suite
pins that too, since the gate check sits right next to it.
"""

import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _isolated_dirs import isolated_data_dirs  # noqa: E402

_TMP = isolated_data_dirs("wb-web-create-gate-")

import wb_accounts  # noqa: E402
import wb_settings  # noqa: E402
import wb_webcreate  # noqa: E402

ACCOUNTS = os.path.join(_TMP.name, "accounts")

#: The answer the deployment recorded: HTTP 401 with this body.
GATE_PAYLOAD = {
    "code": 10034,
    "msg": "CreateConversation: 10034:CODEBUDDY_API_KEY is required but "
           "could not be found",
}
STALE_TOKEN_PAYLOAD = {"code": 11140, "msg": "账号存在异常行为，已被限制使用"}


class _Handler(BaseHTTPRequestHandler):
    status = 200
    payload = {}
    posts = 0

    def _respond(self):
        body = json.dumps(type(self).payload, ensure_ascii=False).encode("utf-8")
        self.send_response(type(self).status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            try:
                type(self).bodies.append(json.loads(self.rfile.read(length)))
            except Exception:
                type(self).bodies.append({})
        type(self).posts += 1
        self._respond()

    def do_GET(self):
        # The pre-flight conversation sweep / reclaim must not hang or fail the
        # round; both call sites swallow the error.
        type(self).posts += 0
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


class Upstream(object):
    """A one-response conversation-create endpoint on 127.0.0.1."""

    def __init__(self, status, payload):
        handler = type("_BoundHandler", (_Handler,),
                       {"status": status, "payload": payload, "posts": 0,
                        "bodies": []})
        self.handler = handler
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.url = "http://127.0.0.1:%d/console/as/conversations/" % self.server.server_port

    def __enter__(self):
        self.thread.start()
        self._patch = mock.patch.object(wb_accounts, "WEB_CONVERSATIONS_URL", self.url)
        self._patch.start()
        return self

    def __exit__(self, *exc):
        self._patch.stop()
        self.server.shutdown()
        self.server.server_close()
        return False

    @property
    def posts(self):
        return self.handler.posts

    @property
    def bodies(self):
        return self.handler.bodies


def make_account(name, models=("deepseek-v4.1-flash",)):
    directory = os.path.join(ACCOUNTS, name)
    os.makedirs(directory, exist_ok=True)
    wb_settings.set_daily_chat_models(directory, list(models))
    return wb_accounts.Account(
        {"uid": "u1", "realm": "intl", "accessToken": "t"},
        path=os.path.join(directory, "acc.json"))


class GateDetectionTests(unittest.TestCase):
    def test_gate_is_the_10034_body(self):
        self.assertTrue(wb_accounts.web_create_gate(
            401, json.dumps(GATE_PAYLOAD)))
        self.assertTrue(wb_accounts.web_create_gate(
            403, json.dumps(GATE_PAYLOAD)))

    def test_gate_is_the_marker_without_a_parsable_body(self):
        self.assertTrue(wb_accounts.web_create_gate(
            401, "CreateConversation: 10034:CODEBUDDY_API_KEY is required"))

    def test_other_401s_are_not_the_gate(self):
        # A stale token or a ban has to keep meaning "the credential is broken".
        self.assertFalse(wb_accounts.web_create_gate(
            401, json.dumps(STALE_TOKEN_PAYLOAD)))
        self.assertFalse(wb_accounts.web_create_gate(
            401, json.dumps({"code": 401, "msg": "invalid token"})))
        self.assertFalse(wb_accounts.web_create_gate(401, "Unauthorized"))

    def test_gate_needs_an_auth_status(self):
        self.assertFalse(wb_accounts.web_create_gate(
            500, json.dumps(GATE_PAYLOAD)))
        self.assertFalse(wb_accounts.web_create_gate(None, json.dumps(GATE_PAYLOAD)))

    def test_message_names_the_cause_and_rules_out_relogin(self):
        text = wb_accounts.web_create_gate_message()
        self.assertIn("10034", text)
        self.assertIn("CODEBUDDY_API_KEY", text)
        self.assertIn("不需要重新登录", text)
        self.assertNotIn("HTTP", text)


class BaselineOnlyTests(unittest.TestCase):
    """No variant fallback is wired, on purpose.

    scripts/probe_web_conversation.py tried the whole wb_webcreate matrix against
    the live upstream: the *baseline* shape was accepted (HTTP 200, created and
    released). Until a variant is the one that answers, trying the others would
    only add requests to a request that is already refused, so the create path
    must keep sending exactly one shape — the baseline.
    """

    def test_create_sends_exactly_the_baseline_body(self):
        acc = make_account("baseline-only", models=("m1",))
        with Upstream(401, GATE_PAYLOAD) as upstream:
            acc._daily_chat_web_upstream()
            self.assertEqual(upstream.posts, 1)
            body = upstream.bodies[0]
        self.assertEqual(body, wb_webcreate.baseline_body("Hi", "m1"))
        self.assertEqual(list(body), ["prompt", "model", "conversationOrigin",
                                      "plugins"])


class GateRoundTests(unittest.TestCase):
    def run_web(self, acc):
        sleeps = []
        with mock.patch.object(acc, "refresh") as refresh, \
                mock.patch.object(acc, "report_activity") as report, \
                mock.patch.object(wb_accounts.time, "sleep",
                                  side_effect=sleeps.append):
            res = acc._daily_chat_web_upstream()
        return res, sleeps, refresh, report

    def test_gate_stops_after_one_attempt(self):
        acc = make_account("gate", models=("m1", "m2", "m3"))
        before = dict(acc.to_dict())
        with Upstream(401, GATE_PAYLOAD) as upstream:
            res, sleeps, refresh, report = self.run_web(acc)
            self.assertEqual(upstream.posts, 1, "the gate must not be retried")
        self.assertFalse(res["ok"])
        self.assertEqual(res["gate"], "codebuddy-api-key")
        self.assertEqual(res["code"], 10034)
        self.assertEqual(res["phase"], "create")
        self.assertEqual(res["attempts"], 1)
        self.assertEqual(res["models_tried"], ["m1"])
        self.assertEqual(sleeps, [], "a refused create must not pay the backoff")
        refresh.assert_not_called()
        report.assert_not_called()
        self.assertFalse(acc.account_banned)
        self.assertIsNone(acc.last_daily_chat)
        self.assertEqual(dict(acc.to_dict()), before,
                         "the gate must not write anything to the account")

    def test_gate_payload_on_a_200_is_also_a_gate(self):
        acc = make_account("gate-200")
        with Upstream(200, GATE_PAYLOAD) as upstream:
            res, _sleeps, _refresh, _report = self.run_web(acc)
            self.assertEqual(upstream.posts, 1)
        self.assertFalse(res["ok"])
        self.assertEqual(res.get("gate"), "codebuddy-api-key")

    def test_stale_token_keeps_the_old_error_shape(self):
        acc = make_account("stale")
        with Upstream(401, STALE_TOKEN_PAYLOAD) as upstream:
            res, _sleeps, refresh, _report = self.run_web(acc)
            self.assertEqual(upstream.posts, 1)
        self.assertFalse(res["ok"])
        self.assertNotIn("gate", res)
        self.assertTrue(res["error"].startswith("HTTP 401"), res["error"])
        refresh.assert_not_called()

    def test_retryable_failures_still_retry(self):
        # Guards the gate branch against swallowing the transient-failure path.
        acc = make_account("transient", models=("m1", "m2"))
        with Upstream(500, {"code": 500, "msg": "AgentOS API error"}) as upstream:
            res, _sleeps, _refresh, _report = self.run_web(acc)
            self.assertEqual(upstream.posts, 2, "a 5xx is still worth a retry")
        self.assertFalse(res["ok"])
        self.assertNotIn("gate", res)


class GateStepTests(unittest.TestCase):
    GATE_RESULT = {"ok": False, "attempts": 1, "phase": "create",
                   "gate": "codebuddy-api-key", "code": 10034,
                   "error": wb_accounts.web_create_gate_message()}

    def strip(self, acc, web_result, base=None):
        with mock.patch.object(acc, "daily_chat_web", return_value=dict(web_result)):
            return acc._daily_chat_web_step(dict(base or {}), web=True)

    def test_gate_message_replaces_the_http_status(self):
        acc = make_account("step-gate")
        before = dict(acc.to_dict())
        result = self.strip(acc, self.GATE_RESULT)
        self.assertFalse(result["ok"])
        self.assertEqual(result["gate"], "codebuddy-api-key")
        self.assertEqual(result["error"], wb_accounts.web_create_gate_message())
        self.assertIn("10034", result["msg"])
        self.assertNotIn("已重试", result["msg"])
        self.assertNotIn("网页通道失败：HTTP", result["error"])
        self.assertIsNone(acc.last_daily_chat)
        self.assertEqual(dict(acc.to_dict()), before)

    def test_non_gate_failure_keeps_the_old_wording(self):
        acc = make_account("step-plain")
        result = self.strip(acc, {"ok": False, "attempts": 2, "phase": "create",
                                  "error": "HTTP 500: boom"})
        self.assertFalse(result["ok"])
        self.assertNotIn("gate", result)
        self.assertEqual(result["error"], "网页通道失败：HTTP 500: boom（已重试 1 次）")

    def test_success_marks_the_day_as_done(self):
        acc = make_account("step-ok")
        result = self.strip(acc, {"ok": True, "attempts": 1, "status": "completed",
                                  "chunks": 3, "elapsed_ms": 1200},
                            base={"ok": True})
        self.assertTrue(result["ok"], "a successful web leg must not fail the day")
        self.assertIn("成功完成", result["msg"])
        self.assertNotIn("gate", result)
        self.assertIsNotNone(acc.last_daily_chat)


if __name__ == "__main__":
    unittest.main()
