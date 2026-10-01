---
name: decision-cards
description: >-
  Decision Cards — structured asks that actually reach the owner. Decision cards surface a decision, blocker, or critical fact as a window the owner can click, type into, and answer — without burying it in terminal prose that stays unread.
---

# Decision Cards — structured asks that actually reach the owner

Decision cards surface a decision, blocker, or critical fact as a window the owner can click, type into, and answer — without burying it in terminal prose that stays unread.

When you need an agent to ask a human something, or alert them to something important, a decision card is the way: it pops a window, carries full detail (facts never truncate), and an answer from the owner **steers the session that raised it**. For Claude Code, that means resuming on the owner's answer without ever touching the terminal.

## Why not prose?

A decision buried in paragraphs is a stream event — it scrolls by and is forgotten. A decision card is a state — it stays until answered, routes to the owner across whatever surface they're actually looking at, and drives the session forward.

```bash
# Don't do this:
echo "I found three options — let me know which you prefer"
echo "Waiting for your input..."

# Do this instead:
adk decide ask "One-line headline — the whole ask" \
  --summary "One or two lines explaining the facts." \
  --fact "something I measured" \
  --fact "another measurement" \
  --option "keep|Keep current behavior|No changes needed" \
  --option "fix|Apply the fix|Solves the problem, needs restart" \
  --recommend fix --default keep --deadline 4h --urgency high
```

The card opens a window, returns immediately, and you keep working. The owner answers at their own pace, and the session picks it up.

## Raise a card from the CLI

```bash
adk decide ask "Your question here" \
  --summary "Context in one or two lines" \
  --fact "A measured fact" \
  --option "optionA|Label for A|What happens if you pick A" \
  --option "optionB|Label for B|What happens if you pick B" \
  --recommend optionA --default optionA --urgency high
```

Manage the queue:

```bash
adk decide list                    # what's waiting
adk decide show <card-id>          # one card in full
adk decide answer <card-id> <opt>  # pick an option
adk decide steer <card-id> "text"  # send free-form guidance (card stays open)
adk decide window                  # re-open the card window
adk decide cancel <card-id>        # withdraw a card you no longer need
```

**Use `|` as the separator, not `:`** — a Windows path in an option (`C:\something`) silently mangles with the colon form.

## Raise a card from an ADK agent

Agents have the `decisions` tool category by default:

```python
ask_human(
    title="Your question",
    summary="Context",
    options=[{"key": "optionA", "label": "Label", "consequence": "What happens"}],
    facts=["fact1", "fact2"],
    urgency="high",
    recommend="optionA",
    default="optionA",
    wait_seconds=0,
    deadline_seconds=3600
)
```

For non-blocking cards, set `wait_seconds=0` and poll with:

```python
check_human(card_id="")  # answered yet, or has the owner typed anything?
```

Withdraw a card once you've resolved it yourself:

```python
withdraw_card(card_id, reason="I figured it out")
```

List your open cards:

```python
list_my_cards()
```

## The five fields that make a card decidable

A card that cannot be answered from a phone lock screen is not finished.

1. **Title** — one line, the whole ask. If it needs two, raise two cards.
2. **Facts** — what you *measured*, not what you guess. This is the single highest-value field. Never assume; measure.
3. **Options** — each with a **consequence**. A label says what a choice is called; a consequence says what actually happens to their machine. Only the consequence lets someone decide without reading code.
4. **`--recommend`** — say what you would do. A card with no recommendation pushes your thinking onto the owner.
5. **`--default`** — what happens if they never answer. **Required**, because a card is only safe to ignore if you've specified what happens by default. An agent that cannot name a default is not blocked on a decision, it is blocked on doing its own thinking.

## When to raise one — the bar is HIGH

**A card is an interruption. The default is to keep working.**

Raise one only when ALL three of these hold:

1. The readings lead to **materially different work** (not just a different style), **AND**
2. **A wrong guess costs real work** to undo — time, money, data, or an outward-facing action, **AND**
3. **You cannot settle it yourself** by reading code, measuring, or picking the conventional default.

If any clause fails: decide it yourself, say so in one line, keep going.

**Do raise a card for:**
- Two designs, both defensible, days of work diverge
- About to delete, force-push, post publicly, or spend money
- Genuinely blocked: missing credential, ambiguous requirement you cannot resolve
- Critical facts the owner should know and might miss

**Do NOT raise a card for:**
- A naming choice or a library with an obvious default → decide it
- "Should I continue?" or "Does this look right?" → that is the anti-pattern
- Reporting that you finished something → that is a sentence
- Permissions prompts or idle signals → that is what the harness handles

## What the owner sees

A borderless window carrying the title, facts in full, and options as buttons.

**Buttons** click to answer. **A reply box** lets them type guidance (like "do X first"); typing steers the session while keeping the card open. **Terminal controls** let them focus the terminal, open a new one at the card's directory, or copy the path. **Keyboard shortcuts:** `1`–`9` pick an option, `Enter` takes the recommendation, `Ctrl+Enter` sends the reply, `Esc` closes.

**Focus is graded:** `critical` and `high` urgency take focus; `normal` appears without stealing keystrokes.

An answer reaches the session through three tiers, tried in order:

1. **Direct** — if the session is running, the answer is written as if typed.
2. **In-flight** — for ADK agents, injected between tool calls.
3. **Mailbox** — always written; drained on the next prompt.

For Claude Code, a **stop hook holds the turn open for ~50s after raising a card**. Answer inside that window and the session **keeps working on your answer** without ever going to the terminal. Miss it and nothing is lost; the answer arrives at the next prompt.

## Verify it

```bash
adk decide --self-test                  # store, validation, expiry, steer, answer round-trip
```

Each self-test proves it can still fail — and the round-trip checks exist because an answer that reaches nobody is worse than no card at all.

## Honest capability

**Focusing brings the window forward, not a specific tab.** Windows Terminal exposes no supported way to activate one tab of an existing window from outside — that is why the card prints the tab title instead of pretending. Terminal input is measured, not assumed.

Rendering controls that quietly do nothing would be a silent no-op pattern, and this channel refuses that — it grows a control, or it does not.

## Answering from somewhere other than this machine

The CLI above needs nothing running. `adk decide list`, `show`, `answer` and
`cancel` read the card store on local disk directly, so a fresh install works
immediately.

Reaching those cards from **anywhere else** — a terminal UI, a browser
extension, a phone, a web dashboard — goes through a small local HTTP daemon
that serves the same store:

```bash
adk harness serve          # serves the card store on 127.0.0.1:8362
```

Start it once and leave it running. It writes a bearer token to
`~/.aither/harness_token` on first start, and clients read that file. It refuses
to start without one — there is no "no auth in dev" mode.

**Nothing starts it for you, and that is deliberate.** Registering a background
service on someone's machine is a bigger ask than installing a CLI, so the
decision is yours to make rather than the installer's. The cost of that choice
is worth naming: if the daemon is not running, remote surfaces have no cards to
show, and an empty list looks exactly like "nothing is waiting on you". A client
that cannot reach it should say so and name this command — if one silently shows
you an empty list instead, that is a bug in the client, not an empty queue.

Bind it to loopback only. The same daemon also exposes session and filesystem
endpoints, so it is not something to publish on a public hostname; reach it from
another device through a private tunnel or an authenticated proxy that fronts it,
never by opening the port.

## Part of the fabric

Decision cards are one half of a human-facing control plane. The other is secure credential input (for API keys and passwords). Both treat the owner as the source of truth, not a checkbox to click, and both stay durable — the owner's decision or secret survives a session restart.
