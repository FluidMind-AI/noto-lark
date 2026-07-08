# Noto Lark — User Guide

Noto is a knowledge agent that lives in your Lark workspace. It has
read your organization's wiki, documents, and chats, and it can answer
questions, draft and edit documents, and handle your calendar and
expenses — all from a Lark message.

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

## 4. Expenses

DM Noto a **receipt** (photo or PDF) or type *"/expense 42 SGD taxi to
client meeting yesterday."* Noto reads it and adds a row to your
reimbursement Base — category, amount, currency, date — left pending
for the usual approval. It'll ask if it can't read the amount or
currency. *(This feature is optional and off by default; your admin
enables it.)*

## 5. Noto learns from you

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

## 6. Handy commands

| Command | What it does |
|---|---|
| `/login` | DMs you a one-time link to the admin Control Center |
| `/expense <text>` | Log an expense from a text description |
| `/forget <name>` | Remove a personal preference Noto learned |
| `/help` | Quick reminder of what Noto can do |

Most things don't need a command — just ask in plain language.

## 7. Good habits, at a glance

- **Be specific** — name the project, doc, person, or topic.
- **Share your calendar with Noto** and **keep your travel on it** —
  timezones just work after that.
- **Say the timezone** for cross-border scheduling ("3pm ET").
- **Click citations** before trusting an answer for anything important.
- **React 👍** to answers you'd want repeated — you're training it.
- **Correct it** when it's wrong ("feedback: …") — that's how it improves.
