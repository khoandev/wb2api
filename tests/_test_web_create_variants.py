# -*- coding: utf-8 -*-
"""The web-create request matrix: baseline stays byte-identical, overlays rotate.

    python tests/_test_web_create_variants.py

Background: the intl daily activity's web channel died on 2026-11-16 with
HTTP 401 / code 10034 ("CODEBUDDY_API_KEY is required"). Nothing in that answer
says which part of the request is refused, and the #90 capture cannot be redone
without an interactive login, so wb_webcreate holds both the shipped body and
the shapes a probe may try after it.

Two regressions this suite exists for:

  1. **Baseline drift.** The gateway sends `wb_webcreate.baseline_body(...)` on
     every install. If a refactor reshapes it "while touching the variants",
     every working deployment silently changes what it asks the web console for.
  2. **Credential leakage into a probe.** Variant headers overlay the account's
     auth headers, so the overlay itself must never carry Authorization /
     X-Api-Key / X-User-Id / cookies.
"""

import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import wb_webcreate as W  # noqa: E402

BASELINE_KEYS = ["prompt", "model", "conversationOrigin", "plugins"]
LABELS = [
    "baseline",
    "no-plugins",
    "no-model",
    "origin-workbuddy-web",
    "origin-codebuddy-web",
    "desktop-headers",
    "no-model-no-plugins",
]
CREDENTIAL_HEADERS = ("authorization", "x-api-key", "x-user-id", "cookie",
                      "x-device-token", "x-access-token")
PROMPT = "Hi"
MODEL = "deepseek-v4.1-flash"


class BaselineBodyTests(unittest.TestCase):
    def test_baseline_is_the_captured_web_app_shape(self):
        self.assertEqual(
            W.baseline_body(PROMPT, MODEL),
            {
                "prompt": PROMPT,
                "model": MODEL,
                "conversationOrigin": "workbuddy-app",
                "plugins": [{"name": "weixinpay",
                             "marketplace": "codebuddy-builtin"}],
            })
        self.assertEqual(list(W.baseline_body(PROMPT, MODEL)), BASELINE_KEYS)

    def test_baseline_returns_independent_dicts(self):
        first = W.baseline_body(PROMPT, MODEL)
        second = W.baseline_body(PROMPT, MODEL)
        first["plugins"].append({"name": "other", "marketplace": "x"})
        first["model"] = "changed"
        self.assertEqual(second["plugins"],
                         [{"name": "weixinpay", "marketplace": "codebuddy-builtin"}])
        self.assertEqual(second["model"], MODEL)

    def test_baseline_is_json_serializable(self):
        text = json.dumps(W.baseline_body(PROMPT, MODEL), ensure_ascii=False)
        self.assertIn("conversationOrigin", text)


class VariantMatrixTests(unittest.TestCase):
    def setUp(self):
        self.body = W.baseline_body(PROMPT, MODEL)
        self.variants = W.variants(self.body, domain="www.workbuddy.ai",
                                   ide_version="1.2.3")
        self.by_label = {v.label: v for v in self.variants}

    def test_labels_are_stable_and_unique(self):
        self.assertEqual([v.label for v in self.variants], LABELS)
        self.assertEqual(len(set(LABELS)), len(LABELS))

    def test_first_entry_is_the_baseline_request(self):
        head = self.variants[0]
        self.assertEqual(head.label, "baseline")
        self.assertEqual(head.body, self.body)
        self.assertEqual(head.headers, {})
        self.assertIsNot(head.body, self.body, "variant body must be a copy")

    def test_every_entry_gets_its_own_body(self):
        for variant in self.variants:
            self.assertIsNot(variant.body, self.body,
                             "%s shares the caller's dict" % variant.label)
        self.by_label["baseline"].body["plugins"].append({"name": "x",
                                                          "marketplace": "y"})
        self.assertEqual(
            W.baseline_body(PROMPT, MODEL)["plugins"],
            [{"name": "weixinpay", "marketplace": "codebuddy-builtin"}])

    def test_overlays_only_remove_the_documented_keys(self):
        self.assertEqual(set(self.by_label["baseline"].body), set(BASELINE_KEYS))
        self.assertEqual(set(self.by_label["no-plugins"].body),
                         set(BASELINE_KEYS) - {"plugins"})
        self.assertEqual(set(self.by_label["no-model"].body),
                         set(BASELINE_KEYS) - {"model"})
        self.assertEqual(set(self.by_label["no-model-no-plugins"].body),
                         {"prompt", "conversationOrigin"})

    def test_origin_overlays_change_only_the_origin(self):
        for label, origin in (("origin-workbuddy-web", "workbuddy-web"),
                              ("origin-codebuddy-web", "codebuddy-web")):
            variant = self.by_label[label]
            self.assertEqual(set(variant.body), set(BASELINE_KEYS))
            self.assertEqual(variant.body["conversationOrigin"], origin)
            self.assertEqual(variant.body["plugins"], self.body["plugins"])
            self.assertEqual(variant.body["prompt"], PROMPT)
            self.assertEqual(variant.body["model"], MODEL)

    def test_desktop_headers_keep_the_baseline_body(self):
        variant = self.by_label["desktop-headers"]
        self.assertEqual(variant.body, self.body)
        self.assertEqual(variant.headers,
                         W.desktop_headers("www.workbuddy.ai", "1.2.3"))

    def test_every_body_is_json_serializable(self):
        for variant in self.variants:
            self.assertIsInstance(
                json.loads(json.dumps(variant.body, ensure_ascii=False)), dict)


class DesktopHeaderTests(unittest.TestCase):
    def test_markers_come_from_the_desktop_profile(self):
        headers = W.desktop_headers()
        for name in ("X-Agent-Intent", "X-Agent-Purpose", "X-Agent-Type",
                     "X-IDE-Name", "X-IDE-Type", "X-Private-Data", "X-Product"):
            self.assertIn(name, headers)

    def test_no_credential_header_is_ever_added(self):
        for variant in W.variants(W.baseline_body(PROMPT, MODEL),
                                  domain="www.workbuddy.ai", ide_version="1.2.3"):
            lowered = {name.lower() for name in variant.headers}
            self.assertEqual(lowered & set(CREDENTIAL_HEADERS), set(),
                             "%s leaks a credential header" % variant.label)

    def test_optional_markers_are_omitted_when_unknown(self):
        headers = W.desktop_headers()
        self.assertNotIn("X-Domain", headers)
        self.assertNotIn("X-IDE-Version", headers)

    def test_optional_markers_are_filled_when_known(self):
        headers = W.desktop_headers("www.workbuddy.ai", "1.2.3")
        self.assertEqual(headers["X-Domain"], "www.workbuddy.ai")
        self.assertEqual(headers["X-IDE-Version"], "1.2.3")


class CallerBodyTests(unittest.TestCase):
    def test_variants_derive_from_the_body_they_are_given(self):
        # A regression here would mean the probe tests something the gateway
        # does not send: the caller's body is the source of truth.
        body = W.baseline_body("Custom prompt", "claude-sonnet-4-5")
        body["extraField"] = "kept"
        variants = W.variants(body)
        for variant in variants:
            if variant.label in ("no-model-no-plugins", "no-model",
                                 "no-plugins"):
                continue
            self.assertEqual(variant.body["prompt"], "Custom prompt")
            self.assertEqual(variant.body["extraField"], "kept")


if __name__ == "__main__":
    unittest.main()
