# Lark API battle scars

Every one of these cost real debugging hours. They are ordered roughly
by how likely you are to hit them. Format: symptom → cause → fix.
Cross-reference `docs/setup.md` for the Console click-paths.

---

## 1. Config changes silently do nothing until you publish a version

**Symptom:** You added a scope / subscribed an event / changed
availability in the Developer Console. API calls still return
permission errors; events still don't arrive. Nothing in any log.

**Cause:** Console changes apply to the *draft* app. The tenant runs
the last *published* version.

**Fix:** **Version Management & Release → Create a version → Submit →
admin approves.** Every time. Make "did you publish?" your first
debugging question for any Console-adjacent problem, because it is the
answer disturbingly often.

## 2. DMs never reach the webhook even though `im:message` is granted

**Symptom:** Group messages arrive; 1-on-1 messages to the bot vanish.
No webhook POST, no error, nothing.

**Cause:** DM delivery is gated behind a *separate* scope,
`im:message.p2p_msg`. Lark drops p2p messages before delivery without
it — silently. Group delivery has its own gate
(`im:message.group_msg`).

**Fix:** Grant `im:message.p2p_msg` (and `im:message.group_msg`), then
publish a version (see #1).

## 3. You cannot share anything with the bot

**Symptom:** Docs/Base/folder shared "with the app" are invisible;
drive and wiki APIs return empty lists or 403 with a valid tenant
token.

**Cause:** Lark resources are never shareable with app identities.
This is unlike Slack, Notion, and Google — stop looking for the
"share with bot" button; it does not exist.

**Fix:** Access flows through a *real user account* you create for the
bot. Share resources with that user, then mint user-scoped OAuth
tokens for it (`tools/lark_oauth.py`). The app (tenant) token is only
for messaging, cards, contacts, Bitable-the-app-owns, and — weirdly —
mail (see #6).

## 4. `im/v1` APIs reject your user token

**Symptom:** Listing chats or fetching messages with a user
`access_token` returns permission errors, even though the same token
happily reads docs.

**Cause:** User OAuth has no `im:*` scopes at all — Lark simply
doesn't issue them to user tokens. All `im/v1` surfaces (chats,
messages, message resources) are tenant-token territory.

**Fix:** Use `tenant_access_token` for anything under
`/open-apis/im/v1/`. See `tools/chat_corpus.py` for a worked example.

## 5. Mail bodies fail to decode with "Incorrect padding"

**Symptom:** `base64.b64decode` on a mail body raises cryptic padding
errors, or decodes to garbage on some messages and fine on others.

**Cause:** Lark Mail bodies are **URL-safe base64** — the alphabet
uses `-` and `_` instead of `+` and `/`. Messages that happen not to
contain those characters decode fine, which is why it "sometimes
works" and drives you insane.

**Fix:** `base64.urlsafe_b64decode`, always. See
`tools/lark_mail.py`.

## 6. Mail body reads fail with the user token

**Symptom:** You have `mail:user_mailbox.message.body:read` granted
and a healthy user token; body fetches still 403.

**Cause:** The mail message and body scopes are **tenant-token
scopes**, despite reading a *user's* mailbox. The user token never
carries them.

**Fix:** Call the mail message/body endpoints with
`tenant_access_token` from the app credentials. Only the folder scope
(`mail:user_mailbox.folder:read`) is user-side.

## 7. You get subjects but no senders (or vice versa)

**Symptom:** Message metadata comes back with a subject but the sender
and recipients are missing — no error, the fields just aren't there.
Or your parsing code finds no `from` / `email` fields.

**Cause:** Two separate traps. (a) Address fields are gated by their
*own* scope, `mail:user_mailbox.message.address:read`, distinct from
the message scope. (b) The API's field names are `head_from` /
`mail_address`, not `from` / `email` as any reasonable person would
guess.

**Fix:** Grant the address scope (and publish, #1), and parse
`head_from` / `mail_address` from the response.

## 8. Tenant scope granted, still 403: the data range

**Symptom:** A tenant mail scope shows as granted in the Console, the
version is published, and the API still returns a permission error
indistinguishable from a missing scope.

**Cause:** Some tenant-token scopes (mail is the big one) have a
second, independent knob: a **data range / member range** per API
panel, configured separately from the scope grant. Default range can
be nobody.

**Fix:** On the Permissions screen, open each relevant panel
("Retrieve emails", "Retrieve email body", …) → **Filter by
condition** → add the bot user to the member range → Save. Per panel.

## 9. The mailbox address is not `user_info.email`

**Symptom:** Mail API calls addressed to the email returned by
`/authen/v1/user_info` fail or hit the wrong mailbox.

**Cause:** `user_info.email` is the *login* email of whoever
authorized the token — which can be a personal address tied to the
same Lark seat, not the tenant-issued mailbox.

**Fix:** Configure the tenant mailbox address explicitly
(`lark.noah_mailbox_email` in `notolark.yaml`) and use it to address
the mail API. Never derive it from `user_info`.

## 10. Free/busy returns `[]` for a window you know is busy

**Symptom:** Free/busy queries over a narrow window (say, 14:00–15:00)
return an empty list even though an event sits squarely inside it.

**Cause:** The freebusy endpoint behaves unreliably for narrow
windows. Also, returned timestamps are **true UTC with a `Z`
suffix** — even for events created in your local timezone — so naive
local-time comparison silently mismatches everything.

**Fix:** Query the *whole day*, parse timestamps as UTC, and intersect
with your target window yourself. See `tools/lark_calendar.py`.

## 11. A pasted image arrives as `message_type: "post"`, not `"image"`

**Symptom:** Your image handler works for image-only messages but
never fires when someone pastes a screenshot together with text in one
message.

**Cause:** Text + image in a single message is a rich-text **`post`**
message. The image is buried in the post's content tree, not delivered
as an `image` message.

**Fix:** Handle `post` as a first-class case: walk the content
structure and collect every `image_key`, then fetch each via the
message-resource API (tenant token + `im:resource`, see #4). Don't
branch solely on `message_type == "image"`.

## 12. Card buttons render but clicks go nowhere

**Symptom:** Interactive cards display fine; pressing a button does
nothing. No webhook traffic.

**Cause:** `card.action.trigger` is subscribed in the **Callback
Configuration** panel — a *separate tab* from Event Subscription, even
though it takes the same URL. Subscribing events alone does not route
card callbacks.

**Fix:** Console → Events & Callbacks → **Callback Configuration** →
set the request URL → subscribe `card.action.trigger` → publish (#1).

Related: **ACK card callbacks in under 3 seconds** or Lark shows the
user an error and may retry. Return the acknowledgment immediately and
push the actual work (LLM calls, doc writes) onto a queue/thread. A
card callback handler that does slow work inline will look broken to
users no matter how correct it is.

## 13. CardKit 2.0 rejects your button layout

**Symptom:** Cards that used an `action` element block fail schema
validation or render without buttons after "upgrading" to CardKit 2.0.

**Cause:** CardKit 2.0 dropped the `action` container element that 1.x
used to group buttons.

**Fix:** Put buttons directly as top-level body elements, or nest them
in a `column_set` for horizontal layout. See `tools/lark_cards.py`.

## 14. Console refuses to verify a webhook URL that provably works

**Symptom:** `curl` against your Request URL returns 200 with the
right challenge behavior, but the Console keeps saying the URL is
invalid, forever.

**Cause:** The Console caches a failed verification state per URL and
can refuse to re-verify it even after you've fixed the endpoint. The
cache eventually expires (observed: weeks).

**Fix:** Give it a *different* URL so it's treated as new — e.g. stand
up a temporary Cloudflare quick tunnel, verify that, and switch back
to your permanent URL later once the cache has expired. Also make sure
your endpoint answers GET/OPTIONS, not just POST — some verifier paths
probe with those.

## 15. launchd jobs fail with "command not found"

**Symptom:** A script that works perfectly in your shell dies
instantly under launchd; logs show `command not found` for `python3`,
`jq`, `tailscale`, or anything Homebrew-installed.

**Cause:** launchd runs jobs with a bare `PATH`
(`/usr/bin:/bin:/usr/sbin:/sbin`). Your `.zshrc` is never sourced.

**Fix:** Reference every binary by absolute path in anything a plist
runs (`/opt/homebrew/bin/jq`, the venv's `python3`, …), or activate
the venv explicitly at the top of the script as the shipped
`tools/lark-bot-run.sh` does. Also scrub inherited environment when
relevant — the shipped runner unsets `CLAUDE*`/`ANTHROPIC_*` vars so a
bot restarted from inside an interactive CLI session doesn't inherit
session state into its own `claude -p` subprocesses.

## 16. OAuth quietly dies after a week of inactivity

**Symptom:** The bot works for days, then every user-token call starts
failing after a quiet stretch (holidays are a classic).

**Cause:** Access tokens live 2 hours; refresh tokens live 7 days. If
nothing refreshes within the window, the refresh token expires and the
grant is dead — manual re-authorization required.

**Fix:** Run the keepalive job (`deploy/com.noto.larkkeepalive.plist`,
daily) which refreshes tokens even when the bot is idle. If you're
already past the window: `tools/lark_oauth.py --identity <slot> url`
and re-authorize in a browser logged in as the bot user. Note the
identity slots (`operator`, `noah`) keep **separate token files** —
re-authing one never touches the other; that's by design, don't
"unify" them.

## 17. The bot can't see any chat history from before it joined

**Symptom:** The chat corpus only contains messages newer than the day
you added the bot to each group.

**Cause:** Platform limitation — the API cannot retrieve group history
from before the bot's join unless the group had **"Allow new members
to view chat history"** enabled at (or before) join time.

**Fix:** Enable that toggle in each group's settings *before* adding
the bot. For groups where it's too late: remove the bot, enable the
toggle, re-add. There is no API workaround.

## 18. Regenerating webhook secrets bricks the running subscription

**Symptom:** After a recovery/reinstall, every inbound webhook fails
decryption or token validation.

**Cause:** Someone clicked **Regenerate** on the Encrypt Key /
Verification Token while re-reading them from the Console. The Console
signs with the new values immediately; your config still has the old
ones (or vice versa).

**Fix:** When recovering credentials, **copy — never regenerate**. The
existing values are always readable in the Console (Credentials &
Basic Info for app id/secret, Encryption Strategy for the webhook
pair); losing the local file is a 10-minute copy job, not a disaster.
Related recovery facts: the whole app definition (scopes, events,
callback config, redirect URLs) lives on Lark's side and survives your
machine dying, and OAuth tokens are re-issuable in ~5 minutes per
identity. Keep `tools/backup-credentials.sh` output somewhere
off-machine anyway to skip the clicking.

## 19. Write scope ≠ delete capability (by design here)

Not a Lark quirk — a project guardrail worth knowing before you fight
it. The user scopes are full read+write (`docx:document`,
`drive:drive`, `sheets:spreadsheet`) because the bot creates docs,
sheets, and folders. But the code contains no path that deletes a
top-level Lark object (doc, file, folder, message, wiki node, Bitable
record), and `lark_client.assert_no_lark_delete()` scans the tools at
startup and aborts if one is ever introduced. Block-level edits
*within* a doc are allowed (they preserve Lark's native edit history
as an audit trail); whole-object deletion is not. If a feature seems
to require deleting a Lark object, redesign the feature — the guard
will win.

## Raw markdown appearing in docs (`**bold**`, `###` as literal text)

**Symptom:** docs the bot creates or edits show literal `**`, `###`
and `-` characters instead of formatting.

**Cause:** Lark text blocks hold styled *runs*, not markdown. Any
writer that pushes LLM output as plain text runs (especially the
single-body-block update path used for in-place edits) leaves the
markdown tokens visible. Heading/bullet *blocks* also can't exist
inside a text block, so a naive converter can't fix an edit path.

**Fix (implemented):** every writer feeds text through one inline
parser (`_md_inline_segments`: links, `**bold**`, `*italic*` → styled
runs). The single-text-block path additionally emulates structure —
heading lines render as bold, `-`/`*` list markers become `•`.
If you add a new write path, use these helpers; never push raw LLM
text into `TextRun.content`.
