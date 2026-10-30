# Plan: guidebook research as a subagent (multi-agent)

**Goal:** move knowledge-base retrieval out of the travel agent into a dedicated research
subagent. The travel agent asks a question and gets back a short, cited answer. The subagent
does the searching and reading in its own context, which is discarded afterwards.

*Status: proposal, 2026-10-30. Tracks issue #34 (milestone #11 in [PLAN.md](../PLAN.md)).
Numbers measured on the local POC database (23 threads, 59 turns).*

## Contents

- [1. Why now](#1-why-now)
- [2. Which multi-agent pattern](#2-which-multi-agent-pattern)
- [3. Design](#3-design)
- [4. Costs and risks](#4-costs-and-risks)
- [5. Evaluation](#5-evaluation)
- [6. Roadmap](#6-roadmap)
- [7. Open questions](#7-open-questions)

## 1. Why now

Guidebook research is the agent's heaviest work:

| Measured on 59 turns | Value |
|---|---|
| Turns that used the guidebooks | 21 |
| Guidebook tool calls per research turn | median **8**, max 14 |
| Guidebook result text per research turn | median **~6.6k tokens**, max ~14.7k |
| Guidebook share of all tool-result text in those turns | **90%** |

Memory M2 (turn compaction) already removes these results from *later* turns. The remaining cost
sits **within the research turn**:
- **Every step resends everything.** Each of the ~8 main-agent calls carries the full ~4.4k-token
  prefix (rules, skills index, user memories, 13 tool schemas), plus every page read so far.
  That's roughly 25–35k input tokens per research turn spent re-reading guidebook text.
- **The main agent reasons over raw pages.** It also has to juggle search strategy, citations,
  the user's constraints, memory tools and the itinerary schema all at once.

A subagent is CoALA's separation of concerns. The travel agent keeps working memory for the
*conversation*; the research agent has a short-lived working memory for *one question*, and only
its conclusion crosses back.

## 2. Which multi-agent pattern

LangChain v1 describes a few multi-agent patterns:

| Pattern | How it works | Fit for guidebook research |
|---|---|---|
| **Subagents as tools** (supervisor) | The main agent calls another agent like a tool, gets its result, and stays in control of the conversation | **Chosen.** Research is a bounded task with a clear question in and an answer out; the user always talks to the travel agent |
| Handoffs | Control passes to another agent, which then talks to the user directly | Wrong shape: the research agent shouldn't own the conversation, see the user's memories, or present itineraries |
| Router / parallel fan-out | A router sends the request to one or several specialists, and their outputs are combined | Overkill as a separate component, but the main agent gets fan-out for free: it can call the subagent tool several times in one step (e.g. Rome food, transport and museums in parallel) |

**Decision:** one research subagent behind one tool, `ask_guidebook`. The same pattern later
covers web research (#33, `fetch_page`) as a second research subagent.

## 3. Design

```
┌ Travel agent (main) ─────────────────────────────────────┐
│ prompt: rules · skills · user memories · trips           │
│ tools: ask_guidebook · flights · hotels · memory · trips │
│        · recall_conversations · load_skill · present_…   │
└───────────────┬──────────────────────────▲───────────────┘
   question,    │                          │ findings with citations
   destination  ▼                          │ (~0.5–1.5k tokens)
┌ Guidebook research agent (subagent) ─────┴───────────────┐
│ prompt: search strategy · citation rules · output format │
│ tools: list_guidebooks · search_guidebooks ·             │
│        read_guidebook_pages                              │
│ no checkpointer, no store, no user context               │
└──────────────────────────────────────────────────────────┘
```

**The tool the main agent sees:**

```python
ask_guidebook(question: str, destination: str | None = None) -> str
```
- **What it returns:** findings in compact text: a direct answer, specific details (names,
  opening days, prices, travel times), and a citation for each, like `(Lonely Planet Italy,
  p. 412)`.
- **When a destination isn't covered:** it says so plainly, so the main agent can fall back to
  general knowledge, labelled as such (today's rule).
- **Structured internally:** the subagent ends with a Pydantic `GuidebookFindings` (`answer`,
  `facts[{text, book, page}]`, `covered: bool`, `gaps`). Here `create_agent(response_format=…)`
  is fine, because the subagent's run always ends with findings. The tool renders the result to
  text for the main agent.

**The subagent:**
- **Tools:** the three guidebook tools, moved off the main agent, unchanged.
- **Prompt:**
  - the "Guidebooks" section of today's main prompt;
  - step 1 of the `itinerary-builder` skill (search for the book's own suggested routes first);
  - the citation rules;
  - "answer only the question; quote specifics; never invent pages".
- **Stateless and isolated.** No checkpointer, store or `UserContext`, so it can't read or write
  user memory. Text in a guidebook page (or, later, a web page) can't make it save anything. It
  can only shape the findings the main agent reads, which narrows the injection risk from #33.
- **Bounded.** `ModelCallLimitMiddleware` caps it at ~10 model calls per question. On hitting the
  cap, it returns what it has and lists the gaps.
- **Model.** `get_chat_model()` by default, so it shares the main agent's 429 retries and pacing.
  An optional `GUIDEBOOK_AGENT_MODEL` setting lets it use a cheaper model (open question).

**Wiring:**
- **Tracing.** Invoked with `observability.run_config(user_id, thread_id)` plus metadata
  `agent: guidebook_research`. Langfuse then nests the subagent's run under the tool call in the
  same trace and session (a CLAUDE.md rule for every agent invocation).
- **Status lines.**
  - Users currently see `🔧 search_guidebooks(…)` lines while the agent researches.
  - The tool forwards the subagent's tool calls through LangGraph's custom stream
    (`get_stream_writer`), and `openai_compat` adds `"custom"` to its stream modes, so users
    still see what is being read.
  - The lines are indented under `🔧 ask_guidebook(…)`.
- **Compaction (M2).** `ask_guidebook` results are small and are the evidence behind the answer,
  so the compaction middleware keeps the latest few (say 3) from earlier turns, like
  `load_skill`, instead of dropping them.
- **Skills.**
  - `itinerary-builder`, `food-and-culture` and `budget-planner` reference `search_guidebooks` /
    `read_guidebook_pages` directly; they switch to `ask_guidebook`.
  - `eval_skills.py` must still pass 9/9.
- **Rollout flag.** `GUIDEBOOK_SUBAGENT=true|false` (default false during K1) chooses between
  the three tools and the subagent tool on the main agent, so both designs can be evaluated
  side by side.

## 4. Costs and risks

| Risk | Effect | Mitigation |
|---|---|---|
| **More model calls** | The subagent's final answer and the main agent's follow-up add ~1–2 calls per research question, against the provider's ~20 requests/min | Measure in K0/K1. The subagent's calls are much smaller (~1.5k prefix vs ~4.4k), so tokens fall even if calls rise slightly. A cheaper subagent model would avoid the shared limit |
| **Latency** | Extra sequential calls per question | Parallel questions in one step. Accept up to +20% per research turn (acceptance criterion) |
| **Lost detail** | The main agent no longer sees raw pages: opening days, booking rules and prices could be dropped from the answer | Structured `facts` with specifics are required; the eval checks named entities and citations |
| **Wrong or invented citations** | The main agent can't verify pages it never saw | The subagent may cite only pages it read in this run, checked in code against its tool results before returning |
| **Less visible research** | Users see one tool call instead of the reading | Custom-stream status lines (above) |
| **Two prompts to keep in step** | Guidebook guidance lives in the subagent | It moves rather than being copied; the main prompt keeps only "use ask_guidebook for covered destinations" |

## 5. Evaluation

Same approach as the memory roadmap: measure first, then change.

**New `scripts/eval_guidebooks.py`**, about 8 questions with known answers in the guidebooks:
- Rome: closing days of a museum, cheap eats near Termini.
- Big Island: the best snorkelling near Kona, the Mauna Kea summit rules.
- Peru: getting to Machu Picchu, altitude advice for Cusco.
- Thailand and Switzerland: one each.
- An uncovered destination (Lisbon), where the answer must say it isn't covered.

Each question runs through the main agent, with these checks:
- **Correct:** the answer names the expected entity.
- **Cited:** at least one citation, with a page within ±2 of the known page.
- **Honest:** for the uncovered destination, it says the books don't cover it.
- **Cost:** main-agent input tokens, total input tokens, model calls, and wall time per question.

**Regression checks:**
- `eval_skills.py`, since skills change.
- `eval_memory.py --only long_thread --runs 1`, since the Rome chat researches.
- `e2e_branching.py`, since the request path and streaming change.

**Acceptance for switching the default on (K2):**
- [ ] Correctness and citation pass rate **no worse** than K0
- [ ] **Main-agent** input tokens per research question **−50% or better**
- [ ] **Total** input tokens per research question (main + subagent) **lower** than K0
- [ ] Model calls per research question **at most +2**; wall time **at most +20%**
- [ ] `eval_skills.py` 9/9, `long_thread` passes, `e2e_branching.py` passes

## 6. Roadmap

Each phase is one PR, like the memory roadmap.

| Phase | Scope | Effort |
|---|---|---|
| **K0 Baseline** | `scripts/eval_guidebooks.py` on today's design; record numbers here | ½ day |
| **K1 Subagent behind a flag** | `agent/research.py` (subagent + `ask_guidebook` tool + `GuidebookFindings`); citation check; call cap; tracing metadata; custom-stream status lines; compaction keeps recent findings; `GUIDEBOOK_SUBAGENT` setting (+ `.env.example`); eval with the flag on and off | 1–2 days |
| **K2 Default on** | Flip the default once §5's acceptance holds; move skill references to `ask_guidebook`; remove guidebook guidance from the main prompt; update architecture.html and the memory explainer | ½ day |
| **K3 Web research (with #33)** | A second research subagent with `fetch_page`, same contract, plus the #33 guard: memory can't be written from page text, because research agents have no memory tools | with #33 |

## 7. Open questions

- **Subagent model:** same `gpt-6-luna`, or a cheaper one (fewer tokens, its own rate limit)? K1
  measures both if a second model is available on OpenRouter.
- **One research agent or one per source?** Guidebooks and web pages could share one agent with
  both tool sets. Separate agents keep each prompt focused and the untrusted web text isolated;
  the plan assumes separate.
- **Should the subagent see the trip?** Passing destination and dates lets it check seasonal
  closures. Passing the user's preferences would personalise research but breaks the isolation in
  §3. Proposal: destination and dates only, as tool arguments.
