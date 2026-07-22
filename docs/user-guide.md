# Noto Lark — User Guide

Noto is a knowledge agent that lives in your Lark workspace. It has
read your organization's wiki, documents, and chats, and it can answer
questions, draft and edit documents, and handle your calendar,
reminders, and expenses — all from a Lark message.

**How to talk to Noto:** DM it directly, or @mention it in a group. In
a DM you don't need to @mention — just talk. Noto answers in the same
chat, and for anything substantial it streams its progress so you can
see it working.

This is a living document — when Noto gains a capability, it's added
here. If something below doesn't work as described, tell Noto
"feedback: …" and it goes to the review queue.

---

## 1. Ask it anything about your organization's knowledge

Noto answers from **your own documents** — the wiki, internal docs,
files, and group-chat knowledge it has synced — with **clickable
citations** back to the source.

- *"What's our process for onboarding a new client?"*
- *"Summarize the Q3 planning doc."*
- *"What did we decide about the pricing change in the #product chat?"*

**Get better answers:**
- Be specific — name the project, doc, person, or topic.
- If Noto says it can't find something, it genuinely isn't in the
  synced knowledge (the corpus refreshes on a schedule, so a doc
  edited today is typically searchable after the next nightly sync).
- Every answer links its sources — click through to verify.

## 2. Draft and edit documents

- **Create:** *"Make a doc summarizing this thread"* or *"draft a
  one-pager on our refund policy."* Noto writes it as a proper Lark
  doc and sends the link.
- **Edit by link:** paste **any** Lark doc link (including wiki pages)
  and say what to change — *"add a section on X", "tighten the third
  bullet."* Edits happen in place, preserving the doc's native edit
  history.
- Noto **never deletes** a doc, file, or record — if something needs
  removing, it'll tell you to do it yourself.

## 3. Calendar — add events by text or screenshot

Two ways:
- **Type it:** *"add to my calendar lunch with the vendor on Wednesday
  1pm at the office."*
- **Screenshot it:** send a screenshot of an email or chat with the
  details and say *"add to my calendar."*

Noto reads it, **asks for anything missing** (time, or the venue for an
in-person meeting), checks your calendar for **duplicates and
conflicts** (and lets you decide "add anyway" / "skip"), then creates
the event on **your** calendar with reminders **1 hour and 15 minutes**
before. Events are editable afterward — you can move them or add people.

### ⏰ Timezones — help Noto get them right

Noto can know each person's home city, so *"3pm"* means 3pm **your**
time. Two things make it bulletproof:

- **Say the timezone when it matters:** *"call at 3pm Eastern"* always
  wins, no matter where you are.
- **Put your travel in your calendar** — and **share your calendar with
  Noto** (Reader access is enough). Add an event titled **"Travel:
  Tokyo"** (or "Trip to London", "✈️ HK", "In New York") for the days
  you're away. Then when you ask Noto to schedule something during that
  trip, it uses the **local** timezone of where you actually are — so a
  meeting you book from Tokyo lands at the right hour, not your home
  hour. Without this, Noto assumes you're in your home city.

> **Team habit worth forming:** keep your travel on your calendar with
> the destination in the title. It's the difference between Noto
> nailing your meeting times on the road and quietly getting them wrong.

**Invitees:** Noto only ever invites people inside your organization to
events (configurable by email domain) — never outside contacts.

## 4. Reminders — your personal task list

Say *"noto remind me to …"* and it goes on a private Lark task list
called **"Noto — \<your name\>"** (created the first time you use it,
visible only to you and Noto — it shows up in your Lark **Tasks** app,
and you can tick things off there directly).

- *"remind me to call Joe tomorrow at 3pm"* → task with a **Lark
  alert at 3pm your time** (say *"3pm Eastern"* and that wins — same
  timezone rules as the calendar, travel events included).
- *"remind me to send the report on Friday"* → dated task, no clock
  alert; it shows up in Friday's morning digest.
- *"remind me to reply to Kim"* → undated task on your list.

**Every morning around 8am your time**, Noto DMs you a digest of
what's **due today** plus anything **overdue** — and stays quiet if
there's nothing pending.

Managing the list in chat:
- *"what's on my list?"* — see your open reminders.
- *"done with the Joe call"* — tick one off (Noto asks which one if
  several match).

Noto never deletes tasks — completed items stay in the list's history.
*(This feature is optional and off by default; your admin enables it.)*

## 5. Expenses

DM Noto a **receipt** (photo or PDF) or type *"/expense 42 SGD taxi to
client meeting yesterday."* Noto reads it and adds a row to your
reimbursement Base — category, amount, currency, date — left pending
for the usual approval. It'll ask if it can't read the amount or
currency. *(This feature is optional and off by default; your admin
enables it.)*

## 6. Noto learns from you

- **Thumbs-up what's good:** react 👍 on a great answer (or reply
  "perfect"). Noto remembers that question-and-answer as a hint for
  similar questions later — it still does fresh research every time,
  but it knows what worked for the team.
- **Corrections become lessons:** tell Noto *"feedback: …"* (or just
  correct it) and it goes to a review queue where an admin turns
  recurring corrections into standing behavior.
- **Personal preferences:** if you correct Noto the same way more than
  once, it can remember that just for **you** and DMs you when it does.
  Reply **`/forget <name>`** to undo it anytime.
- **Per-user memory** is DM-only and private to you — Noto won't use
  your personal context in group chats.

## 7. Handy commands

| Command | What it does |
|---|---|
| `/login` | DMs you a one-time link to the admin Control Center |
| `/expense <text>` | Log an expense from a text description |
| `/forget <name>` | Remove a personal preference Noto learned |
| `/help` | Quick reminder of what Noto can do |

Most things don't need a command — just ask in plain language.

## 8. Good habits, at a glance

- **Be specific** — name the project, doc, person, or topic.
- **Share your calendar with Noto** and **keep your travel on it** —
  timezones just work after that.
- **Say the timezone** for cross-border scheduling ("3pm ET").
- **Click citations** before trusting an answer for anything important.
- **React 👍** to answers you'd want repeated — you're training it.
- **Correct it** when it's wrong ("feedback: …") — that's how it improves.

## Your email, answered and drafted

### Ask about your own inbox (`/mail`)
In a **private 1:1 DM** with the bot: `/mail did they ever reply about
the contract?` — or naturally: "check my email — what did I promise
them?" The bot searches YOUR mailbox only (hybrid keyword + semantic
search, whole threads), and answers with dates and senders cited.
Nobody can query anyone else's mailbox — not even admins.

### Auto-drafted replies (review cards)
When an email arrives with you in the **To:** field (never CC, never
lists) and it actually needs an answer, the bot drafts a reply and DMs
you a **review card**: the subject, what they wrote, the draft, and a
**confidence score** (green = routine, safe to send · orange = skim it ·
red = needs your eyes) plus a "what's missing" note.

Buttons: **Send** (goes out as-is — reply-all, inside the same thread,
with your signature) · **Discard** (deletes the draft) · **Edit**
(opens Lark Mail). Or just **reply to the card in chat**: say "send" or
"discard", or describe any change ("shorter, and propose Tuesday") and
the bot redoes the draft and sends a fresh card.

Drafts follow the **house playbook** — response patterns mined nightly
from the team's actual sent mail (how you chase, how you decline, how
you negotiate) — then add your personal voice from your own past
replies. Facts it can't verify are never invented; they go to the
card's note instead.

### The playbook (admins)
`/playbook` in a DM shows the learned response patterns; the **admin
panel → Playbook** tab is the full review seat — each entry with the
exact email exchange it was mined from, and Keep / Retire controls.
Retired entries stop influencing drafts immediately.

### Replying to the bot = context
Use Lark's **Reply** on any bot message and your reply carries that
message as context — "and what about for the Berlin office?" continues
that exact conversation, even days later or across bot restarts.
