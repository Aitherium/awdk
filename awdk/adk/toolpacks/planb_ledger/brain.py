"""Plan B Ledger — local language brain.

Natural-language capture ("paid the electric bill", "spent 34.50 on groceries")
parsed into ledger entries by bonsai-27b served by llama.cpp (OpenAI-compatible
/v1/chat/completions, default 127.0.0.1:8090 — the adk `bonsai` profile port).

Doctrine: the model is the garnish, not the meal. If llama.cpp is down or the
model answers garbage, a deterministic pattern parser takes over — capture must
work with tech HALF-down too. Every result says which brain produced it.
"""
from __future__ import annotations

import json
import os
import re

from . import ledger

DEFAULT_ENDPOINT = os.environ.get("PLANB_LLM_ENDPOINT", "http://127.0.0.1:8090")

_SYSTEM = (
    "You turn one sentence about money into JSON. Output ONLY a JSON object, "
    "no prose. Schema: {\"kind\": \"bill\"|\"expense\"|\"income\", "
    "\"desc\": str, \"amount\": number|null, \"bill_name\": str|null, "
    "\"category\": one of %s}. "
    "'paid <name>' with a known bill name -> kind=bill. Money in -> income. "
    "Known bills: %s"
) % (json.dumps(ledger.CATEGORIES), "%s")

_AMOUNT_RE = re.compile(r"\$?(\d{1,6}(?:,\d{3})*(?:\.\d{1,2})?)")


def _fallback_parse(text: str, state: dict) -> dict:
    """Deterministic parser — works with zero model. Patterns over free text."""
    low = text.lower().strip()
    m = _AMOUNT_RE.search(low)
    amount = m.group(1) if m else None

    bill = None
    if any(w in low for w in ("paid", "pay ", "payed")):
        for b in state["bills"]:
            if b["name"].lower() in low:
                bill = b
                break
    if bill is not None:
        return {"kind": "bill", "bill_name": bill["name"], "desc": f"{bill['name']} (bill)",
                "amount": amount, "category": "Bills", "brain": "fallback"}

    income_words = ("paycheck", "got paid", "deposit", "received", "refund", "income")
    kind = "income" if any(w in low for w in income_words) else "expense"
    category = "Income" if kind == "income" else "Other"
    for cat, words in (("Food", ("grocer", "food", "lunch", "dinner", "coffee", "restaurant")),
                       ("Auto", ("gas", "fuel", "car ", "oil change", "uber", "parking")),
                       ("Home", ("home", "repair", "furniture", "rent suppl")),
                       ("Fun", ("movie", "game", "concert", "fun", "bar "))):
        if kind == "expense" and any(w in low for w in words):
            category = cat
            break

    desc = re.sub(r"(spent|paid|got|received|bought|for|on)\b", " ", low)
    desc = _AMOUNT_RE.sub(" ", desc).replace("$", " ")
    desc = re.sub(r"\s+", " ", desc).strip().capitalize() or text.strip()[:60]
    return {"kind": kind, "bill_name": None, "desc": desc, "amount": amount,
            "category": category, "brain": "fallback"}


def _bonsai_parse(text: str, state: dict, endpoint: str, timeout: float) -> dict | None:
    """Ask bonsai via llama.cpp. Returns None on any failure — caller falls back."""
    try:
        import httpx
    except ImportError:
        return None
    bill_names = ", ".join(b["name"] for b in state["bills"]) or "(none)"
    body = {
        "model": "bonsai-27b",
        "temperature": 0.1,
        # Reasoning models spend most of the budget thinking before the JSON
        # appears — 160 tokens measured as all-think/no-answer on 27B Q1_0.
        "max_tokens": 900,
        # llama.cpp grammar-forces valid JSON in content; servers that don't
        # support it 400 and we fall back.
        "response_format": {"type": "json_object"},
        # Capture is a parsing task, not a reasoning task — without this,
        # bonsai (qwen3-family) burned ~450 think-tokens per one-line entry.
        "chat_template_kwargs": {"enable_thinking": False},
        "messages": [
            {"role": "system", "content": _SYSTEM % bill_names},
            {"role": "user", "content": text},
        ],
    }
    try:
        resp = httpx.post(f"{endpoint.rstrip('/')}/v1/chat/completions",
                          json=body, timeout=timeout)
        if resp.status_code != 200:
            return None
        msg = resp.json()["choices"][0]["message"]
        content = (msg.get("content") or "") or (msg.get("reasoning_content") or "")
        m = re.search(r"\{.*\}", content, re.DOTALL)
        if not m:
            return None
        parsed = json.loads(m.group(0))
        if parsed.get("kind") not in ("bill", "expense", "income"):
            return None
        parsed["brain"] = "bonsai-27b"
        return parsed
    except Exception:  # noqa: BLE001 — any model failure means: use the fallback
        return None


def capture(text: str, state: dict, endpoint: str | None = None,
            timeout: float = 90.0) -> dict:
    """Parse free text into a proposed entry. Never raises; never silent-fails.

    The timeout is generous on purpose. A DEAD server refuses instantly, so this
    only bounds a server that is alive but still warming — and the first request
    after a cold start was measured at 20.7s on a 27B model, i.e. the old 20s
    budget guaranteed that the very first capture silently used the fallback
    brain while llama.cpp was answering perfectly well.

    Returns {ok, proposal:{desc, amount_c, type, category, bill_id}, brain,
    needs_amount} or {ok: False, error}.
    """
    text = str(text).strip()
    if not text:
        return {"ok": False, "error": "empty capture"}

    endpoint = endpoint or DEFAULT_ENDPOINT
    parsed = _bonsai_parse(text, state, endpoint, timeout) or _fallback_parse(text, state)

    bill = ledger.find_bill(state, parsed["bill_name"]) if parsed.get("bill_name") else None
    if parsed.get("kind") == "bill" and bill is None and parsed.get("bill_name"):
        bill = ledger.find_bill(state, parsed["bill_name"])

    if bill is not None:
        amount_c = bill["amount_c"]
        if parsed.get("amount"):
            try:
                amount_c = ledger.parse_amount(str(parsed["amount"]))
            except ValueError:
                amount_c = bill["amount_c"]
        proposal = {"desc": f"{bill['name']} (bill)", "amount_c": amount_c,
                    "type": "out", "category": "Bills", "bill_id": bill["id"]}
        return {"ok": True, "proposal": proposal, "brain": parsed["brain"],
                "needs_amount": False}

    amount_c = None
    if parsed.get("amount"):
        try:
            amount_c = ledger.parse_amount(str(parsed["amount"]))
        except ValueError:
            amount_c = None
    etype = "in" if parsed.get("kind") == "income" else "out"
    proposal = {"desc": parsed.get("desc") or text[:60], "amount_c": amount_c,
                "type": etype,
                "category": parsed.get("category") if parsed.get("category")
                in ledger.CATEGORIES else ("Income" if etype == "in" else "Other"),
                "bill_id": None}
    return {"ok": True, "proposal": proposal, "brain": parsed["brain"],
            "needs_amount": amount_c is None}


def brain_status(endpoint: str | None = None) -> dict:
    """Which brain is live right now? Cheap probe for status displays."""
    endpoint = endpoint or DEFAULT_ENDPOINT
    try:
        import httpx
        resp = httpx.get(f"{endpoint.rstrip('/')}/v1/models", timeout=3.0)
        if resp.status_code == 200:
            return {"brain": "bonsai-27b (llama.cpp)", "endpoint": endpoint, "live": True}
    except Exception:  # noqa: BLE001 — probe failure just means fallback mode
        return {"brain": "pattern fallback", "endpoint": endpoint, "live": False}
    return {"brain": "pattern fallback", "endpoint": endpoint, "live": False}
