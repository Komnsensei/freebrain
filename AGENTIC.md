# builderBRO: Defining "Agentic"

---

An **agent** is a system that perceives its environment, makes autonomous
decisions, acts on them, and adapts based on observed results — without a
human driving each intermediate step.

In the context of builderBRO, **agentic** means the LLM has real hands: a
headless Chromium browser it controls directly. It reads pages, decides what
to click, fills forms, submits them, recovers from errors, and chains
multi-step workflows — all inside a single turn, without asking the user
"should I click this?" or "now what?"

## I. Defining "Agentic"
### The four properties of an agentic system

| Property | What it means | How BRO does it | |---|---|---| | **Stateful perception** | Sees the full environment at every step, not just a one-time snapshot | `web_open` returns readable page text + a numbered map of every interactive element | | **Autonomous action** | Clicks, types, submits, scrolls, runs JS — without asking permission | 15 browser tools: `web_open`, `web_click`, `web_type`, `web_select`, `web_key`, `web_submit`, `web_scroll`, `web_eval`, `web_search`, `web_text`, `web_raw`, `web_screenshot`, `web_cookies`, `web_close`, `web_status` | | **Goal-driven planning** | Given a task, it picks the right pattern and executes end-to-end | System prompt teaches RESEARCH, COMPARISON, PURCHASE,

LOGIN, and FORM patterns as reusable recipes | | **Error recovery** | When something breaks, it tries alternatives instead of asking for help | Fallback chain: empty page → `web_text`, bad selector → try by label, headless blocked → `web_raw`, repeated failures → stop and tell user |

### Tool vs. Agent

| | Tool mode (old BRO) | Agentic mode (BRO now) | |---|---|---| | **Search** | Returns raw DuckDuckGo snippets, user picks what to do next | Sees results, evaluates them, opens the best result automatically | | **Browse** | Couldn't browse at all | Navigates, reads content, clicks elements, fills forms | | **Multi-step** | One tool call per turn, user drives every transition | `search → open → read → click → type → submit` in one turn | | **Errors** | Reports failure, user fixes it | Falls back through alternatives, only asks user when truly stuck

| | **Memory** | None between sessions | Persistent Chromium profile (`~/.bro/web-profile`), logins survive |

The core shift is **who makes decisions**. A tool waits for the user to say
"now click button 3." An agent sees the page, knows the goal, and clicks
button 3 itself — then evaluates whether that got it closer to the goal and
adjusts.

```
User: "Find the cheapest RTX 4090 in stock and tell me the price."

### How the agentic loop works in BRO

BRO (one autonomous turn):
  1. web_search "RTX 4090 in stock price 2026"
  2. Reads results, picks 3 retail results (not forum posts, not reviews)
  3. web_open <result 1> → reads page content → extracts "$1,799 at Newegg"
  4. web_open <result 2> → reads page content → extracts "$1,749 at B&H"
  5. web_open <result 3> → reads page content → extracts "$1,829 at Amazon"
  6. Reports to user: "Cheapest is $1,749 at B&H. Sources: [newegg link], [bhphoto link], [amazon link]"
```

The user didn't pick any search result, didn't tell BRO which page to open,
didn't tell it how many to compare — BRO planned and executed every step.

### Where BRO is today vs. next steps

| Capability | Today | Next | |---|---|---| | **Research** | Search → open → read → answer with source URL | Track state across 10+ pages, synthesize findings into a written report with citations | | **Transactions** | Type fields → submit forms (single page) | Handle multi-page checkouts, CAPTCHAs, 2FA acknowledgment | | **Comparison** | Open 2-3 results, extract key data, present table | Aggregate prices across 10+ stores, track historical price from page data | | **Memory** | Persistent browser profile (logins survive sessions) | Research notepad: structured notes the agent keeps during a turn, passed between

turns | | **Safety** | User can ESC to abort, repeated-failure guard | Pre-submit confirmation for purchases, spending limits, domain allowlists |

**The agent should do what a competent human assistant would do** — research
thoroughly, act decisively when the path is clear, and ask only when the
decision carries real risk (spending money, deleting data, irreversible
actions).

*Generated for builderBRO v3.0 — PassionCraft*
