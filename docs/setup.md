# Setup — zero to running

This is the full path from an empty machine to a Lark bot that answers
questions from your company's docs, chats, and Bases. Budget 60–90
minutes, most of it clicking through the Lark Developer Console.

Read the whole thing once before starting. Two facts shape everything
below:

1. **Lark International bots receive messages only via an HTTPS
   webhook.** There is no WebSocket / socket mode. Your always-on box
   needs a public HTTPS endpoint; we use Tailscale Funnel because it
   gives you a stable URL with zero port-forwarding.
2. **Lark never shares resources with app identities.** Unlike Slack or
   Notion, you cannot "share a folder with the bot". Every doc, wiki,
   Drive tree, and Base the agent reads is accessed *as a real user*,
   via user-scoped OAuth tokens. You will create a dedicated Lark user
   account for the bot and authorize it. Plan for this; it is not
   optional.

---

## 1. Prerequisites

- **An always-on machine.** A Mac mini is the reference deployment
  (the `deploy/` launchd jobs assume macOS), but any box that runs
  Python and stays up works — you'll translate the plists to systemd
  units yourself.
- **Python 3.12+** and **[uv](https://docs.astral.sh/uv/)** for
  package management. Do not use pip directly.
- **The `claude` CLI** (or a compatible CLI the agent shells out to
  for synthesis), installed and logged in:

  ```bash
  claude /login
  ```

  The bot invokes it as a subprocess (`claude -p …`); if the CLI can't
  authenticate non-interactively, nothing downstream works.
- **Tailscale** with Funnel enabled on your tailnet (Funnel is a
  per-tailnet admin toggle; check your Tailscale admin console).
- **Lark tenant admin rights** — needed once, to create the custom app
  and approve its scopes. If you aren't the tenant admin, get one on a
  call for steps 4–9.
- **A dedicated Lark user account for the bot** (e.g. a seat named
  after your agent). This is the identity whose eyes the agent reads
  with. Give it access to everything the agent should know: add it to
  the relevant groups, share the Drive root and wiki spaces with it,
  give it a mailbox if you want mail ingestion.

```bash
brew install python@3.12 tailscale jq sqlite3
curl -LsSf https://astral.sh/uv/install.sh | sh
```

## 2. Clone and create the venv

The launchd plists in `deploy/` hardcode the install path. Pick your
path now and stick to it — or accept that you'll edit every plist.

```bash
git clone https://github.com/YOUR-ORG/noto-lark.git ~/noto-lark
cd ~/noto-lark
uv venv --python 3.12
source .venv/bin/activate
uv pip install -r requirements.txt
```

Everything below assumes `LOLABOT_HOME` points at the repo root. The
tools auto-detect it when run from the repo, but the launchd jobs set
it explicitly.

## 3. Get your public webhook URL first

The Console will ask for a Request URL and try to verify it, so know
the URL before you start clicking.

```bash
tailscale login
tailscale funnel --bg --https=443 127.0.0.1:8088   # 8088 = default bot port
tailscale status --json | jq -r '.Self.DNSName'
# → e.g. mybox.tailXXXX.ts.net.
```

Your Request URL is `https://<funnel-host>/lark/webhook` and your OAuth
redirect is `https://<funnel-host>/lark/oauth/callback`. Write both
down. (The bot's runner script, `tools/lark-bot-run.sh`, re-asserts the
funnel on every start, so this survives reboots.)

## 4. Create the custom app

1. Go to <https://open.larksuite.com/> → **Developer Console**
   (Feishu tenants: <https://open.feishu.cn/>; also set
   `lark.base_url` accordingly in `notolark.yaml` later).
2. **Create Custom App** → name it, add an icon → **Create**.
3. Open the app → **Credentials & Basic Info** screen. Copy the
   **App ID** and **App Secret** somewhere safe. These two identify
   the *app*; they are distinct from the webhook secrets in step 6.

## 5. Enable the Bot feature

Left nav → **Add Features** → **Bot** → **Add**. Set the display name
and avatar. Without this feature the app can't appear in chats at all.

## 6. Permissions & Scopes

Left nav → **Permissions & Scopes**. Search for each scope and add it.
Scopes come in two flavors and the Console shows which is which:
**tenant (app) scopes** power the bot process directly via
`tenant_access_token`; **user scopes** are what the bot-user OAuth
grant (step 10) is allowed to request. Some scopes must be added as
*both*.

**Messaging (core — the bot is useless without these):**

| Scope | Type | Why |
|---|---|---|
| `im:message` | tenant | Read messages in the bot's chats |
| `im:message.p2p_msg` | tenant | **DM delivery.** Without it Lark silently drops every 1-on-1 message before your webhook — even with `im:message` granted. Not documented anywhere useful; see `docs/gotchas.md`. |
| `im:message.group_msg` | tenant | Group-chat message delivery (same silent gate, for groups) |
| `im:message.group_at_msg.include_bot:readonly` | tenant | See `@bot` mentions inside group messages |
| `im:message:send_as_bot` | tenant | Reply as the bot |
| `im:chat`, `im:chat:readonly` | tenant | Chat metadata, list chats the bot is in |
| `cardkit:card:write` | tenant | Streaming/interactive reply cards |
| `im:resource` | tenant | Download message attachments (images, files). Needed by `tools/attachment_reader.py`. |
| `im:message.reactions:read` | tenant | Read reactions (thumbs-up feedback → retrieval learning) |

**Docs / Drive / Wiki ingestion (user scopes — granted to the bot user
via OAuth):**

| Scope | Type | Why |
|---|---|---|
| `offline_access` | user | Issues a `refresh_token`; without it the user token dies every 2 hours |
| `wiki:wiki:readonly` | user | Enumerate and read wiki spaces |
| `docx:document` | user | Read doc content + create/edit deliverable docs |
| `drive:drive` | user | Walk the Drive tree, create output folders |
| `sheets:spreadsheet` | user | Read sheets for ingestion, write report sheets |
| `bitable:app` | **both** | App side: manage the bot-provisioned Base. User side: read wiki-mounted Bases (you can't share a Base with an app identity, so reads flow through the bot user). |

**Bitable (structured store):**

| Scope | Type | Why |
|---|---|---|
| `bitable:record` | tenant | Read and update records in the bot's Base |

**Contacts (trust tiers — mapping sender → employee):**

| Scope | Type | Why |
|---|---|---|
| `contact:contact.readonly` | tenant | Resolve senders to directory entries |
| `contact:user.email:readonly` | user | Resolve user emails (calendar invites etc.) |

**Calendar (optional):**

| Scope | Type | Why |
|---|---|---|
| `calendar:calendar` | user | Create events, query free/busy |

**Tasks (optional — per-user reminder lists + morning digest):**

| Scope | Type | Why |
|---|---|---|
| `task:task:read` + `task:task:write` | user | Create/list/complete reminder tasks ("remind me to…"). Write ≠ delete: `lark_tasks.py` has no removal code and the startup delete-scan covers it. |
| `task:tasklist:read` + `task:tasklist:write` | user | Create each user's "Noto — Name" tasklist and add them as editor member (that membership is what makes the list appear in their Lark Tasks app). |

After approving new user scopes, re-OAuth the bot-user identity —
scopes only attach to newly issued tokens.

**Mail (optional — mailbox ingestion):**

| Scope | Type | Why |
|---|---|---|
| `mail:user_mailbox.message:readonly` | **tenant** | List messages |
| `mail:user_mailbox.message.body:read` | **tenant** | Read bodies. Yes, tenant — not user. See gotchas. |
| `mail:user_mailbox.message.address:read` | tenant | Sender/recipient addresses — a *separate* scope from the message scope. Without it you get subjects but no senders. |
| `mail:user_mailbox.folder:read` | user | Folder listing |

**Tenant-scope data ranges (easy to miss):** some tenant mail scopes
have a *second* configuration step. On the same Permissions screen,
each mail panel ("Retrieve emails", "Retrieve email body") has a
**data range / member range** setting. Set it to **Filter by
condition** → member range contains your bot user → Save, for *each*
panel. Granting the scope without setting the range yields permission
errors that look identical to a missing scope.

Principle of least privilege: add nothing beyond what your enabled
features need. Skip the mail and calendar blocks entirely if you don't
use those features.

## 7. Event subscription

Left nav → **Events & Callbacks** → **Event Subscription** tab:

1. **Encryption Strategy**: set an **Encrypt Key** (any strong random
   string) and copy the **Verification Token** shown on the same
   panel. Both go into `credentials.yaml` later — the bot decrypts
   every inbound webhook with the key and validates the token.
2. Subscription mode: **Send notifications to developer's server**.
3. **Request URL**: `https://<funnel-host>/lark/webhook`. The Console
   verifies the URL by POSTing a challenge; this only succeeds once
   the bot is running (step 12). It's fine to save now and re-verify
   later.
4. **Add events**:
   - `im.message.receive_v1` — the bot receives a message
   - `im.chat.member.bot.added_v1` — the bot is added to a chat
   - `im.message.reaction.created_v1` — reactions (feedback learning)

## 8. Callback configuration (separate from events)

Still under **Events & Callbacks**, there is a **second tab** called
**Callback Configuration**. Interactive-card button clicks
(`card.action.trigger`) are subscribed *here*, not in the events tab
— same URL, different screen:

1. Subscription mode: **Send callbacks to developer's server**.
2. Request URL: `https://<funnel-host>/lark/webhook` (the bot's single
   handler routes both).
3. Subscribe to **`card.action.trigger`**.

If you skip this, your cards render but every button click vanishes.

## 9. Redirect URL, then PUBLISH A VERSION

1. Left nav → **Security Settings** (or **OAuth** on some Console
   versions) → **Redirect URLs** → add
   `https://<funnel-host>/lark/oauth/callback`.
2. Left nav → **Version Management & Release** → **Create a version**
   → set availability (which departments can use the bot) → **Submit
   for release**.
3. As tenant admin: **Lark Admin Console** → **App Management** →
   find your app → approve the version and its scopes.

**Nothing you configured takes effect until a version is published and
approved.** This includes *every future change*: add a scope, add an
event, change availability — publish a new version each time, or the
change silently does nothing. This is the single most common "why
isn't it working" cause. Tattoo it somewhere.

## 10. The bot-user OAuth dance

The app identity can send/receive messages (tenant token) but cannot
see a single doc. Reading the corpus requires user tokens for the bot
user you created in step 1.

The code has two OAuth identity *slots* (see `tools/lark_oauth.py`):

- **`operator`** — wiki/docs/drive/sheets/bitable scopes; drives the
  corpus walk and doc writing.
- **`noah`** — mail + calendar scopes (the slot name is baked into the
  code; think of it as "the mailbox identity").

Both slots normally hold tokens for the **same** underlying bot user,
just with different scope bundles, in separate token files
(`lark/user_token.json`, `lark/user_token_noah.json`) so re-authing
one never clobbers the other.

```bash
source .venv/bin/activate

python tools/lark_oauth.py --identity operator url
# → prints an authorization URL.
# Open it in a browser LOGGED IN AS THE BOT USER (not your own account
# — whoever clicks Authorize is whose eyes the agent gets).
# The redirect lands on your funnel callback; if the bot is already
# running it exchanges the code automatically. Otherwise paste it:
python tools/lark_oauth.py --identity operator exchange <code>

# Repeat for the mail/calendar identity if you use those features:
python tools/lark_oauth.py --identity noah url

# Verify:
python tools/lark_oauth.py --identity operator status
python tools/lark_oauth.py --identity noah status
```

Both should report `authorized: true`. Access tokens live 2 hours;
refresh tokens live 7 days and are rolled by the keepalive job
(step 14), so an idle bot doesn't silently lose auth.

**The user-token vs tenant-token split**, because you will trip on it:

| API family | Token |
|---|---|
| `im/v1/*` (chats, messages, resources) | tenant — user OAuth has **no** `im:*` scopes |
| docs / wiki / drive / sheets / user-visible Bases | user (`operator` slot) |
| mail message list + bodies | **tenant** (despite being "a user's mailbox") |
| mail folders | user (`noah` slot) |
| calendar | user (`noah` slot) |

## 11. Config files

```bash
cp notolark.yaml.example notolark.yaml
cp credentials.yaml.example brain/credentials.yaml
chmod 600 brain/credentials.yaml
```

`notolark.yaml` — non-secret config:
- `agent.model` — pins the model the bot answers with, decoupled from
  your CLI default. Deliberate: user-facing behavior shouldn't change
  because you upgraded your personal CLI.
- `lark.base_url` / `lark.tenant_url` — International vs Feishu.
- `corpus.drive_root`, `corpus.wiki_spaces`, `corpus.outputs_folder` —
  what to ingest and where the bot writes its deliverables.
- `h2.*` — feature flags, all shipped **off** (step 15).
- If you keep credentials somewhere non-default, set
  `paths.credentials` to match (the template's home is
  `brain/credentials.yaml`).

`brain/credentials.yaml` — git-ignored secrets: `app_id`,
`app_secret`, `verification_token`, `encrypt_key`, plus a random
`dashboard.key` (`openssl rand -hex 24`). Env vars `LARK_APP_ID`,
`LARK_APP_SECRET`, `LARK_VERIFICATION_TOKEN`, `LARK_ENCRYPT_KEY`
override the file if you prefer nothing on disk.

Sanity check the app credentials:

```bash
python tools/lark_client.py token   # → prints a tenant_access_token
```

## 12. Bring the webhook up and verify

```bash
bash tools/lark-bot-run.sh   # foreground for now; launchd later
```

Then in the Console's Event Subscription screen, click **Verify** on
the Request URL — it should turn green. Confirm from outside:

```bash
lsof -nP -iTCP:8088 -sTCP:LISTEN
curl -sS https://<funnel-host>/lark/webhook -w "\nHTTP %{http_code}\n"
```

DM the bot "ping" from Lark. If nothing arrives at the webhook, check
in order: version published? (step 9) `im:message.p2p_msg` granted?
(step 6) URL verified?

## 13. First sync

Add the bot to the group chats it should know, and **enable "Allow new
members to view chat history" in each group's settings before adding
it** — Lark's API cannot see messages sent before the bot joined
unless that toggle was on. This is a platform limitation, not a bug.

Then ingest the corpus:

```bash
python tools/lark_sync.py           # wiki + docs + drive walk
```

First run takes minutes to an hour depending on corpus size. The
`lark/` directory is an ephemeral cache — the source of truth stays in
Lark; you can delete and re-sync at any time.

## 14. Install the launchd jobs

Three plists in `deploy/` keep the thing alive:

| Job | What | Schedule |
|---|---|---|
| `com.noto.larkbot` | Webhook service (asserts funnel, serves) | KeepAlive |
| `com.noto.resync` | Corpus re-sync | Daily 03:30 |
| `com.noto.larkkeepalive` | OAuth token refresh (keeps the 7-day refresh token rolling even when idle) | Daily 03:00 |

**The plists hardcode `/Users/noto/noto-home` as the install path.**
Edit `ProgramArguments`, `WorkingDirectory`, `LOLABOT_HOME`, and the
log paths in each plist to match *your* clone before loading. Also
remember launchd runs jobs with a bare `PATH` — any script a plist
invokes must reference binaries absolutely (the shipped scripts
already do; keep it that way if you edit them).

```bash
for plist in deploy/com.noto.*.plist; do
  cp "$plist" ~/Library/LaunchAgents/
  launchctl load -w ~/Library/LaunchAgents/"$(basename "$plist")"
done
launchctl list | grep com.noto    # expect all three, exit code 0
```

Logs land under `lark/` (`larkbot.out.log`, `resync.log`, …).

## 15. Feature flags

Everything optional ships **off** in `notolark.yaml`:

```yaml
h2:
  email_summary_notes_enabled: false
  personal_preferences_enabled: false
  expenses_enabled: false
  screenshot_calendar_enabled: false
```

The background jobs run the working tree, so new behavior must stay
inert until you deliberately flip it. Flip one flag at a time, restart
the bot (`launchctl kickstart -k gui/$(id -u)/com.noto.larkbot`), and
watch the logs before flipping the next. Flags that need extra Console
scopes (mail, calendar) require a **new version publish** after you
add the scopes — see step 9, again.

## 16. Done — the checklist

- [ ] `python tools/lark_client.py token` prints a token
- [ ] Console Request URL verified green
- [ ] Both OAuth identities `authorized: true`
- [ ] Bot answers a DM
- [ ] Bot answers an `@mention` in a group
- [ ] First `lark_sync.py` completed
- [ ] All launchd jobs loaded
- [ ] `tools/backup-credentials.sh` run once, zip stored off-machine

When something breaks, read `docs/gotchas.md` before debugging — the
odds your problem is already in there are excellent.

## Mail intelligence (per-user inbox Q&A, playbook, auto-draft)

Config (`lolabot.yaml`):

```yaml
mail:
  internal_domain: "@yourco.com"     # colleagues' domain (optional)
  users:                             # each mail-enabled user
    alice:
      mailbox: alice@yourco.com      # tenant mailbox address
      open_id: ou_xxx                # their Lark open_id (DM target + owner gate)
      authority: 1.0                 # playbook exemplar weight (default 1.0)
  signature:                         # house signature template fields
    company_name: "Your Co"
    site_url: "https://yourco.com/"
    site_label: "www.yourco.com"
    linkedin_url: "https://www.linkedin.com/company/yourco/"
    logo_path: "indexes/mail/assets/logo.png"   # optional inline logo
agent:
  company_name: "Your Co"            # used in drafting prompts
```

Access model (two layers, discovered the hard way — see gotchas):
1. **Read** = tenant token + admin data-range: Console → "Accessible data
   range → Email Message" must include each mailbox.
2. **Write (drafts/send)** = each user's own OAuth: send them
   `lark_oauth.authorize_url("<slug>_mail")` — one click. Scopes:
   `mail:user_mailbox.message:modify` + `:send` must be on the app first.

Pipelines: `tools/mail-nightly.sh` (sync → vectors → playbook mining,
schedule via `deploy/com.noto.mailnightly.plist`), `tools/autodraft-poll.sh`
(every 15 min: new To-addressed mail → review card with Send/Discard/Edit).
Keep the host awake: `deploy/com.noto.caffeinate.plist` — sleeping Macs
skip calendar jobs.
