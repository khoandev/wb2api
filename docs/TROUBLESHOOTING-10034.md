# 10034 / CODEBUDDY_API_KEY on the daily web conversation

`CreateConversation: 10034:CODEBUDDY_API_KEY is required but could not be found`
is the upstream refusing to **create** the web-console conversation that the
international daily activity drives. It is not a broken account: on the same
account at the same moment, the read-only calls still answer `200`.

Observed on 2026-11-16, for every OAuth-only international account:

```
每日活跃对话未完成（网页通道失败：HTTP 401: {"code":10034,
  "msg":"CreateConversation: 10034:CODEBUDDY_API_KEY is required but could not be found"})
```

## 1. What it is, and what it is not

| | |
|---|---|
| Broken credential? | No. `GET /console/as/conversations/` and `GET /console/account` still return `200` for the same bearer. |
| Account ban? | No. A ban is `403` / code `11140` and is recorded as `account_banned`. |
| Token expired? | No. An expired token fails the two GETs above as well. |
| Worth retrying? | No. The same request cannot start working by being sent again. |

The gateway used to dress this up as `网页通道失败：HTTP 401 …（已重试 N 次）`,
which reads like a credential problem. Since the classification landed, the
web leg stops after one attempt and reports the cause instead:

```
每日活跃对话未完成（网页通道被上游收紧（code 10034：需要 CODEBUDDY_API_KEY）：
与账号状态无关，不需要重新登录；请换用带 API Key 的账号，或等上游恢复）
```

**Do not re-login and do not rotate accounts to "clear" this.** Re-login changes
nothing here, and rotation only spreads the same refusal over more accounts.

## 2. Check it yourself (read-only, nothing written upstream)

The probe lives in `scripts/`, which the portable package does not ship — copy it
next to the accounts directory first. Run it on the machine that owns the
egress, not on a laptop: the refusal may be tied to where the request comes from.

```bash
# from the checkout, against a local accounts dir
python3 scripts/probe_web_conversation.py --accounts-dir accounts --uid <uid-prefix> --read-only

# on a source install (e.g. /opt/workbuddy2api-hub)
scp scripts/probe_web_conversation.py <host>:/tmp/
ssh <host> 'cd /opt/workbuddy2api-hub && python3 /tmp/probe_web_conversation.py --accounts-dir accounts --uid <uid-prefix> --read-only'
```

Expected when it is the 10034 gate:

```
account 1a2b3c4d (intl, oauth)
  conversations  HTTP 200  code=0 OK
  profile        HTTP 200  code=0 OK
```

Those two `200`s are the point: the credential works, so the failure is create-only.
The probe prints the uid prefix and header *names* only — never a token.

## 3. If create itself is refused: try the request shapes

```bash
python3 scripts/probe_web_conversation.py --accounts-dir accounts --uid <uid-prefix> --probe --sleep 3
```

This POSTs one create per shape defined in `wb_webcreate.variants()` (baseline,
without the `weixinpay` plugin, without `model`, two other
`conversationOrigin` values, plus the desktop agent headers), **stops at the
first shape that returns a conversation id**, and stops and deletes every
conversation it creates. Read the table:

```
  1. baseline                 HTTP 401  code=10034    CreateConversation: 10034:CODEBUDDY_API…
  2. no-plugins               HTTP 200  code=0                                                   released
  winner: baseline
```

* `winner: baseline` means the shipped request is accepted and no variant is
  needed.
* A different winner means the upstream wants that shape. **Nothing is wired
  automatically**: add the finding to `wb_webcreate` and wire the fallback only
  then. As of 2026-11-16 the probe found `baseline` accepted twice, so a
  recurrence should be reported upstream first, not patched around.
* `winner: none` means every shape is refused, i.e. the gate really is about the
  account type, not the request.

If the probe reports leftovers, delete those conversations in the web console;
the daily activity keeps at most ten active conversations per account.

## 4. Keeping the streak while it is refused

Settings → daily activity → **网页通道不可恢复时改走活跃上报** (settings key
`daily_chat_web_fallback_activity`, **off by default**). With it on, an
unrecoverable web failure — the certificate mismatch from #645 or this 10034 gate
— falls back to the activity report:

* the streak (连登) still lights up, and the day counts as done;
* the daily activity **credits are not granted** — only a real, finished
  conversation awards those;
* a plain `401` (stale token) or `403` / `11140` (ban) is *not* covered: those
  keep failing, so a credential problem is never silently marked as done.

Without the switch nothing changes from before except the message: the day stays
open, and the next scheduled window tries again.

## 5. What to collect for an upstream report

- the exact JSON body of the refusal (`code`, `msg`);
- whether the read-only probe answered `200` (it did, if this is 10034);
- the probe table from section 3, including which shapes were refused;
- the gateway version and the egress network (a source install on a VPS and a
  home connection can be treated differently).

Never include `Authorization`, `Cookie`, `X-Api-Key` or device tokens.
