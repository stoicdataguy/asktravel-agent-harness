# Plan: user feedback → audit tables

**Goal:** every thumbs up / thumbs down a user gives in AskTravel is reliably captured in an
append-only audit table that we own. Each one is linked to the agent turn and trace it rates, and
is visible in Langfuse next to that trace.

*Status: proposal. Findings below were verified against the running stack on 2026-10-28.*

> **Tracing moved to LangSmith (#87).** Read "Langfuse score" below as LangSmith feedback
> (`Client.create_feedback(run_id, key="user_feedback", score=…)`), and the deterministic trace id
> as a run id that the API sets from the message id (`run_id` in the run config).

## Contents

- [What happens today](#what-happens-today)
- [Gaps](#gaps)
- [Design](#design)
  - [Audit schema](#audit-schema)
  - [Capturing feedback](#capturing-feedback)
  - [Linking a rating to its turn and trace](#linking-a-rating-to-its-turn-and-trace)
  - [Langfuse scores](#langfuse-scores)
  - [Retention, privacy and access](#retention-privacy-and-access)
- [Phases](#phases)
- [Testing](#testing)
- [Open questions](#open-questions)

## What happens today

Verified by rating a message in a real browser session:

1. **The rating UI works.** Each assistant message has **Good Response / Bad Response** buttons.
   A thumbs-down opens reasons ("Not factually correct", "Too verbose", "Not helpful", "Didn't
   fully follow instructions", "Refused when it shouldn't have", "Being lazy", "Other"), a 1–10
   detail rating and a free-text comment.
2. **Open WebUI stores it in its own database.** The browser calls
   `POST /api/v1/evaluations/feedback`, then `POST /api/v1/evaluations/feedback/{id}` each time
   the user adds a reason or comment. Each call overwrites the same row in Open WebUI's SQLite
   `feedback` table (inside the `open-webui` Docker volume):

   | Column | Contents (from the probe) |
   |---|---|
   | `type` | `rating` |
   | `data` | `{"rating": -1, "reason": "not_factually_correct", "comment": "prices looked made up", "details": {"rating": null}, "tags": [], "model_id": "travel-agent"}` |
   | `meta` | `{"chat_id": "…", "message_id": "<assistant message id>", "message_index": 2, "model_id": "travel-agent"}` |
   | `snapshot` | the **entire chat** as JSON at the time of rating |
   | `user_id` | Open WebUI's user id (not the email) |

3. **Nothing reaches our side.** There's no copy in our Postgres, no Langfuse score, and the
   agent never learns about it. No feedback has been recorded so far (0 rows).

## Gaps

| # | Gap | Consequence |
|---|---|---|
| G1 | Feedback lives only in Open WebUI's SQLite, inside a container volume | Not queryable with our data; lost if the volume is reset; not covered by the Postgres backups planned for AWS |
| G2 | The row is **overwritten** on every edit and **deleted** when the user removes the rating | No audit trail: we can't tell that a 👎 became a 👍, or that a rating was withdrawn |
| G3 | No link from a rating to the agent's turn | The agent API never records Open WebUI's message id, so a rating can't be joined to its Langfuse trace or checkpoint |
| G4 | Not visible in Langfuse | Traces can't be filtered by "rated badly", and there's no feedback trend per tool or skill |
| G5 | Rated message text exists only in the snapshot blob | Reviewing feedback means parsing a whole-chat JSON |
| G6 | Comments are free text and may contain personal data | Needs a retention and access policy before production |

## Design

```
Open WebUI ──👍/👎──▶ feedback table (open_webui schema in our travel_agent db, done in memory M4b)
                          │  AFTER INSERT/UPDATE/DELETE trigger
                          ▼
                 audit.feedback_events  (append-only, our travel_agent db)
                          │                         ▲ joined on message_id
                          │                         │
                          │            audit.agent_turns  ◀── agent API writes one row per turn
                          ▼                                    (message_id, thread_id, trace_id, status)
              feedback worker ──create_score──▶ Langfuse  (score "user_feedback" on the turn's trace)
```

### Audit schema

A separate Postgres schema `audit` in the `travel_agent` database. Plain relational tables, not
the LangGraph store: the store is agent memory with TTLs (see the memory plan), while audit data
must be append-only and easy to query.

```sql
create schema if not exists audit;

-- One row per agent turn, written by the API (the join key for everything else)
create table audit.agent_turns (
  message_id     text primary key,          -- X-OpenWebUI-Message-Id (assistant message)
  chat_id        text not null,
  thread_id      text not null,             -- "<email>:<chat id>"
  user_email     text not null,
  trace_id       text not null,             -- Langfuse trace id (derived from message_id)
  model          text not null,
  thread_sync    text not null,             -- append | seed | rewind | replace
  tools_called   text[] not null default '{}',
  skills_loaded  text[] not null default '{}',
  status         text not null,             -- ok | rate_limited | error
  started_at     timestamptz not null,
  finished_at    timestamptz
);

-- Append-only history of every feedback change; never updated or deleted by the app
create table audit.feedback_events (
  event_id        bigserial primary key,
  received_at     timestamptz not null default now(),
  action          text not null,            -- created | updated | deleted
  feedback_id     text not null,            -- Open WebUI feedback.id
  owui_user_id    text not null,
  user_email      text,                     -- resolved from Open WebUI's user table
  chat_id         text,
  message_id      text,                     -- joins audit.agent_turns
  rating          smallint,                 -- -1 | 1
  reason          text,                     -- e.g. not_factually_correct
  comment         text,
  detail_rating   smallint,                 -- optional 1-10
  tags            jsonb not null default '[]',
  rated_text      text,                     -- the assistant message, extracted from the snapshot
  prompt_text     text,                     -- the user message it answered
  raw             jsonb not null            -- data + meta as received (snapshot excluded)
);
create index on audit.feedback_events (message_id);
create index on audit.feedback_events (user_email, received_at);

-- Current state per rating (latest event, withdrawn ratings excluded)
create view audit.feedback_current as
select distinct on (feedback_id) *
from audit.feedback_events
order by feedback_id, event_id desc;
-- (callers filter action <> 'deleted')
```

The app's database role gets `INSERT` only on `feedback_events` (plus `INSERT/UPDATE` on
`agent_turns`). A read-only `audit_reader` role is for notebooks and dashboards.

### Capturing feedback

Options considered:

| Option | How | Captures edits/deletes? | Verdict |
|---|---|---|---|
| A. Poll Open WebUI's admin export | Job calls `GET /api/v1/evaluations/feedbacks/all/export` every few minutes and diffs | Only by diffing; misses changes between polls | Good as a **reconciliation** check, not the primary path |
| **B. Move Open WebUI's DB to Postgres + trigger** | Set Open WebUI's `DATABASE_URL` to a new `open_webui` database on our Postgres; a trigger on its `feedback` table writes to `audit.feedback_events` | **Yes, every insert/update/delete, transactionally** | **Recommended** |
| C. Open WebUI plugin (function/filter) | Python plugin inside Open WebUI | No: filters run on chat completions, not on ratings | Not possible |
| D. Fork Open WebUI | Patch the evaluations router | Yes | Too costly to maintain |

**Why B:**
- **Fixes G1 and G2 in one step:** nothing can be rated without the audit row being written in the same transaction.
- **Matches the AWS plan:** Open WebUI moves to RDS there anyway.

**Costs:**
- **Coupling to Open WebUI's schema:** the trigger depends on its `feedback` table. A schema check at startup plus option A's nightly reconciliation catch Open WebUI upgrades that change it.
- **Done in memory M4b (#58):** Open WebUI already runs on our Postgres, in schema `open_webui` of the `travel_agent` database (one database, because a trigger can't write across databases), and the existing SQLite data was copied over with `scripts/migrate_owui_to_postgres.py`.

The trigger is a small PL/pgSQL function. It extracts `rating`, `reason`, `comment`, `details`,
`tags`, `chat_id` and `message_id` from the JSON, pulls the rated message and the prompt before
it from `snapshot`, looks up the user's email, and inserts one event. Because the trigger runs in
Open WebUI's database while the audit table is in ours, either both live in **one database**
(Open WebUI tables in schema `owui`) or the trigger uses `postgres_fdw`. **Recommend one
database, two schemas:** simpler, and a single backup covers both.

### Linking a rating to its turn and trace

- **Record the message id:** Open WebUI already forwards `X-OpenWebUI-Message-Id`, the id of the assistant message being generated, which is the same id a rating carries in `meta.message_id`. The API records it in `audit.agent_turns` at the start of each turn and updates the row at the end with status, tools called and skills loaded.
- **Deterministic trace id:** the Langfuse trace id is derived from the message id (the UUID with dashes removed, 32 hex characters), set through the Langfuse handler's `trace_context`. Any rating then maps straight to its trace without a lookup.
- **Regenerate:** "Try Again" creates a new assistant message id, so each version of an answer gets its own turn row and its own ratings.

### Langfuse scores

- **Worker:** a small worker (or `LISTEN/NOTIFY` on new audit events) calls
  `langfuse.create_score(trace_id=…, name="user_feedback", value=rating, data_type="NUMERIC",
  comment=f"{reason}: {comment}", score_id=f"owui-{feedback_id}")`.
- **Edits and withdrawals:** reusing `score_id` makes edits overwrite the score. A withdrawn rating deletes it.
- **Result:** Langfuse can then filter traces by feedback and chart thumbs-down rate by tool, skill and model. The model switch from Sonnet to gpt-6-luna is the first comparison worth making.

### Retention, privacy and access

- **Retention:** keep audit events **2 years**, then delete. This is deliberately separate from the memory TTLs: feedback is evidence about the product, not agent memory.
- **User deletion requests:** set `comment`, `rated_text` and `prompt_text` to NULL and replace `user_email` with a hash. Keep the rating and reason for aggregate statistics.
- **Snapshots:** the `snapshot` blob stays only in Open WebUI's table and is not copied into audit, to avoid duplicating whole chats.
- **Access:** only `audit_reader` (analysts, notebooks) can read comments. They are never shown to the agent.

## Phases

| Phase | Deliverable | Effort |
|---|---|---|
| **F1 Link turns** | `audit.agent_turns` written by the API; deterministic Langfuse trace id from `X-OpenWebUI-Message-Id`; unit tests | ½ day |
| **F2 Capture** | Open WebUI on Postgres (schema `owui`); `audit.feedback_events` + trigger + `feedback_current` view; roles; demo account re-created | 1 day |
| **F3 Langfuse** | Worker that turns audit events into `user_feedback` scores (create, overwrite, delete) | ½ day |
| **F4 Verify & observe** | Nightly reconciliation against the admin export; startup check that Open WebUI's `feedback` columns still match; notebook section "Feedback" with thumbs-down by reason / tool / skill | ½ day |

## Testing

- **Unit:**
  - payload → audit row mapping, covering every reason, comment, detail rating and missing fields
  - trace-id derivation
  - `agent_turns` written on success, rate limit and error
- **Database:** the trigger against a real Postgres. Insert, update and delete each produce exactly one event; `feedback_current` shows the right state; a withdrawn rating disappears from it.
- **End to end** (`scripts/e2e_feedback.py`, browser through Open WebUI against the real model):
  1. Send a message, then 👎 with "Not factually correct" and a comment. Expect one `created` event and one `updated` event (Open WebUI posts twice), with `message_id` matching `agent_turns`, a correct `thread_id` and `trace_id`, `rated_text` equal to the reply, and the comment stored.
  2. Change it to 👍. Expect an `updated` event; `feedback_current.rating = 1`.
  3. A Langfuse score `user_feedback` exists on that trace with the latest value.
  4. Remove the rating. Expect a `deleted` event, gone from `feedback_current`, and the Langfuse score removed.
  5. Regenerate and rate the new answer. Expect a separate turn and trace.
- **Regression:** existing suites (`e2e_flights`, `e2e_hotels`, `e2e_memory`, `e2e_branching`, `eval_skills`).

## Open questions

- Should a 👎 trigger anything immediately (e.g. a Slack alert), or is weekly review enough?
- Do we want the agent to learn from feedback? See the procedural-memory section of the
  [memory architecture plan](memory-architecture.md): feedback is the main input for improving
  skills.
