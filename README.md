# Noto Lark

**A self-hosted AI knowledge agent for [Lark](https://www.larksuite.com) / Feishu.**
Noto ingests your company's wiki, docs, Drive files and group chats into
local search indexes, then answers your team's questions in chat —
grounded in your own documents, with clickable citations. It also
creates and edits Lark docs on request, turns screenshots and messages
into calendar entries, logs expense receipts into a Base, and learns
from your team's feedback.

Built in production at a real company, extracted into this open
chassis. Everything here has survived daily use — including the parts
of the Lark API that bite (see [docs/gotchas.md](docs/gotchas.md)).

## What it does

- **Grounded Q&A in chat** — DM the bot or @mention it in a group. It
  plans search queries, retrieves from lexical + vector indexes over
  your synced corpus, and answers with links to the exact source docs.
  Injection-hardened: retrieved content is fenced as data, inbound
  messages pass a trust/sanitizer layer, and non-admin users can never
  trigger writes.
- **Doc creation & editing by link** — "make a doc summarizing X",
  or paste any Lark doc link (wiki pages too) and ask for a change.
  Block-level edits preserve the doc's native edit history. The agent
  can never delete a doc, file, folder, message or Base record — a
  startup scan aborts if delete-capable code is ever introduced.
- **Screenshot → calendar** — send a screenshot of an email or chat thread
  with "add to my calendar". It reads the image, asks for anything
  missing (venue required for in-person), checks for duplicates and
  conflicts against the requester's own calendar, then creates the
  entry with 60- and 15-minute reminders. Plain-text requests work
  too: *"add to my calendar dinner with Joe on Wednesday 8pm at COTE"*.
- **Expense logging** — DM a receipt (photo/PDF) or type
  `/expense 42 SGD taxi to client meeting` → a row in your
  reimbursement Base, pending approval.
- **A feedback flywheel** — corrections become *derived lessons* an
  admin reviews before they shape behavior; a 👍 reaction on a good
  answer saves it as a retrieval hint for similar questions (never
  served verbatim); repeated personal corrections become private
  per-user preferences with `/forget` undo.
- **Per-user memory** — DM-only, isolated per user, with tombstones so
  deleted facts stay deleted.
- **An admin panel** — magic-link login from chat (`/login`), feedback
  and lesson review queues, usage analytics, system health (OAuth,
  jobs, worker heartbeat), and ops buttons (resync / restart / tunnel).

## How it's built

Single always-on box (a Mac mini works great). Python, SQLite, local
vector + FTS indexes — no external services beyond Lark and your LLM.
The LLM engine is the [`claude` CLI](https://claude.com/claude-code)
by default; every call goes through one chokepoint
(`noto_research._claude`) so pointing it at another engine is a
one-function change.

```
Lark webhook (Tailscale Funnel)
   └─ lark_bot.py         one worker queue, 3s-ack card callbacks, supervisor
        ├─ noto_agent.py  LLM planner → skills (answer / create_doc /
        │                 edit_doc / add_calendar_entry / clarify)
        ├─ noto_research  query planning → hybrid retrieval → cited synthesis
        ├─ screenshot_calendar / expenses    vision flows
        └─ admin_panel.py /admin SPA
nightly resync: wiki + docs + chats → doc_index (FTS) + embeddings (vectors)
```

Safety rails you get for free: no-delete scan at startup, trust tiers
(admin / member / external) on every inbound message, prompt-injection
fencing on retrieved content, operator-confirmed block edits, rate-limit
backoff, push alerts to a chat when background jobs fail, and a worker
supervisor so the bot can't die silently.

## Quick start

**Using Noto** (for your team): [docs/user-guide.md](docs/user-guide.md) — what it can do and how to get the most out of it.

**Setting it up** (for the admin):
1. Read [docs/setup.md](docs/setup.md) — the full Console click-path
   (scopes, events, the version-publish trap) and machine setup.
2. `cp notolark.yaml.example notolark.yaml` and
   `cp credentials.yaml.example credentials.yaml`, fill them in.
3. Run the first sync, start the bot, DM it a question.

Budget an afternoon for the Lark Console part — the permission model
is the hardest thing about this project, which is exactly why the
click-path is documented step by step.

## Status & lineage

This is the generalized chassis of a production agent that runs a
specific company's operations daily. The private deployment and this
repo share a core; fixes flow here as they're battle-tested there.
Issues and PRs welcome.

## License

[Apache-2.0](LICENSE). © FluidMind AI.
