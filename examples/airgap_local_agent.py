#!/usr/bin/env python3
"""Air-gapped local agent: summarise a document corpus with a LOCAL model, cite pages.

The awdk-on-awnix example (AFRL Step 3 harness, gaps G10/G11). It runs an awdk
``LLMRouter`` against an OpenAI-compatible llama.cpp server on loopback, with the
awdk egress guard sealing the process: any non-loopback connection is refused
before a byte leaves, and the refusal is counted in the evidence file.

    python examples/airgap_local_agent.py --corpus DIR --out evidence.json
        [--base-url http://127.0.0.1:8199/v1] [--model ID] [--max-docs 8]
        [--question "..."] [--no-seal]

Corpus: ``*.txt`` / ``*.md`` files in DIR. A page is the text between form-feed
(``\\f``) separators, numbered from 1 (a file with none is one page).

Evidence JSON (``--out``)::

    {schema:1, model, base_url, rows:[{claim, source, page}], sha256_inputs:{file: sha},
     wall_s, ttft_s, egress_violations:[{detail, caller}], air_gap:{mode, sealed},
     verdict: ok|fail|egress-blocked|no-model|no-input|unsealed, detail}

Exit codes: 0 ok (>=1 validated row, zero egress), 1 fail (bad/empty model output,
a citation outside the corpus) or egress blocked, 2 no model reachable / bad input /
the process could not be sealed (an adk without the egress guard; --no-seal overrides).

Defaults come from the environment the awnix unit ships (``/usr/lib/awdk/awdk.env``):
``AITHER_LLM_BASE_URL`` (default http://127.0.0.1:8199/v1) and ``AITHER_MODEL``.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_BASE_URL = "http://127.0.0.1:8199/v1"
DEFAULT_QUESTION = ("List the key factual claims in this document. For each claim give the "
                    "page it appears on.")
EXIT_OK, EXIT_FAIL, EXIT_NO_MODEL = 0, 1, 2
MAX_DOC_CHARS = 12000


# ---- air gap -------------------------------------------------------------


def seal_process(enable: bool) -> Tuple[str, bool]:
    """Install the awdk egress guard. Returns (mode, sealed).

    With a configured enforcer (AITHER_AIR_GAP / AITHER_AIR_GAP_CONFIG) that one is
    used. Otherwise, unless ``enable`` is False, a strict loopback-only enforcer is
    created for this process: the example must never be the thing that leaks.
    """
    try:
        from adk.compliance import air_gap as ag
        from adk.compliance import egress_guard as eg
    except ImportError:
        return "unavailable", False
    enf = ag.get_air_gap_enforcer()
    if not enf.is_enforced() and enable:
        enf = ag.AirGapEnforcer(enabled=True, mode="strict")
        ag.set_enforcer(enf)
    if enf.is_enforced():
        eg.install_egress_guard()
    st = eg.status()
    installed = st.get("installed") or {}
    # Only STRICT refuses. An audit-mode enforcer records a violation and lets the
    # dial through, so an audit process is NOT sealed, however the guard is patched.
    sealed = (bool(st.get("enforced")) and st.get("mode") == "strict"
              and bool(installed.get("socket") or installed.get("httpx")))
    return str(st.get("mode", "disabled")), sealed


def _violation_count() -> int:
    """Violations the process enforcer has recorded so far (0 without a guard)."""
    try:
        from adk.compliance import egress_guard as eg
    except ImportError:
        return 0
    try:
        return int(eg.status().get("violations") or 0)
    except Exception:  # noqa: BLE001
        return 0


def _violations_since(baseline: int) -> List[Dict[str, Any]]:
    """Violations recorded after ``baseline`` (audit mode records without raising)."""
    n = _violation_count() - baseline
    if n <= 0:
        return []
    try:
        from adk.compliance import air_gap as ag
        return list(ag.get_air_gap_enforcer().get_violations(limit=n))
    except Exception:  # noqa: BLE001 - the count alone still fails the run
        return [{"detail": f"{n} violation(s) recorded"}]


def _is_block(exc: BaseException) -> bool:
    try:
        from adk.compliance.air_gap import AirGapViolation
    except ImportError:
        return False
    seen = set()
    cur: Optional[BaseException] = exc
    while cur is not None and id(cur) not in seen:
        if isinstance(cur, AirGapViolation):
            return True
        seen.add(id(cur))
        cur = cur.__cause__ or cur.__context__
    return False


def _caller(exc: BaseException) -> str:
    """First frame of this file in the traceback: who asked for the egress."""
    frames = traceback.extract_tb(exc.__traceback__)
    here = os.path.abspath(__file__)
    mine = [f for f in frames if os.path.abspath(f.filename) == here]
    f = (mine or frames or [None])[-1]
    return f"{Path(f.filename).name}:{f.name}:{f.lineno}" if f else "?"


# ---- corpus ----------------------------------------------------------------


def load_corpus(corpus: Path, max_docs: int) -> List[Dict[str, Any]]:
    docs = []
    for p in sorted(corpus.iterdir()):
        if not p.is_file() or p.suffix.lower() not in (".txt", ".md"):
            continue
        raw = p.read_bytes()
        text = raw.decode("utf-8", errors="replace").replace("\r\n", "\n")
        pages = text.split("\f")
        docs.append({"id": p.name, "sha256": hashlib.sha256(raw).hexdigest(),
                     "pages": pages})
        if len(docs) >= max_docs:
            break
    return docs


def build_prompt(doc: Dict[str, Any], question: str) -> str:
    parts, used = [], 0
    for i, page in enumerate(doc["pages"], 1):
        chunk = page.strip()[: max(0, MAX_DOC_CHARS - used)]
        if not chunk:
            continue
        parts.append(f"[page {i}]\n{chunk}")
        used += len(chunk)
        if used >= MAX_DOC_CHARS:
            break
    body = "\n\n".join(parts)
    return (f"Document: {doc['id']}\n\n{body}\n\n{question}\n"
            'Answer ONLY with JSON: {"rows": [{"claim": "...", "page": <int>}]}')


_JSON_RE = re.compile(r"\{.*\}", re.S)


def parse_rows(text: str, doc: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Validated rows + problems. A row cites a page that exists in THIS document."""
    m = _JSON_RE.search(text or "")
    if not m:
        return [], [f"{doc['id']}: model output has no JSON object"]
    try:
        data = json.loads(m.group(0))
    except ValueError as e:
        return [], [f"{doc['id']}: model JSON unparseable: {e}"]
    rows, problems = [], []
    for r in (data.get("rows") or []) if isinstance(data, dict) else []:
        claim = str((r or {}).get("claim", "")).strip()
        try:
            page = int((r or {}).get("page"))
        except (TypeError, ValueError):
            problems.append(f"{doc['id']}: row without an integer page: {r!r}")
            continue
        if not claim:
            problems.append(f"{doc['id']}: empty claim")
        elif not 1 <= page <= len(doc["pages"]):
            problems.append(f"{doc['id']}: page {page} outside 1..{len(doc['pages'])}")
        else:
            rows.append({"claim": claim, "source": doc["id"], "page": page})
    return rows, problems


# ---- model -----------------------------------------------------------------


async def discover_model(base_url: str, timeout: float) -> str:
    import httpx
    async with httpx.AsyncClient(timeout=timeout) as c:
        r = await c.get(f"{base_url.rstrip('/')}/models")
        r.raise_for_status()
        data = r.json().get("data") or []
        if not data:
            raise RuntimeError("server lists no models")
        return str(data[0].get("id") or "")


async def ask(router: Any, prompt: str, model: str) -> Tuple[str, Optional[float]]:
    """Stream one completion. Returns (text, seconds to first token)."""
    from adk.llm import Message
    t0 = time.perf_counter()
    ttft: Optional[float] = None
    out: List[str] = []
    async for chunk in router.chat_stream(
            [Message(role="system", content="You extract cited claims. Reply with JSON only."),
             Message(role="user", content=prompt)],
            model=model, temperature=0.0, max_tokens=768):
        if chunk.content:
            if ttft is None:
                ttft = time.perf_counter() - t0
            out.append(chunk.content)
    return "".join(out), ttft


async def run(args: argparse.Namespace) -> Tuple[int, Dict[str, Any]]:
    ev: Dict[str, Any] = {"schema": 1, "model": None, "base_url": args.base_url, "rows": [],
                          "sha256_inputs": {}, "wall_s": None, "ttft_s": None,
                          "egress_violations": [], "problems": []}
    mode, sealed = seal_process(not args.no_seal)
    ev["air_gap"] = {"mode": mode, "sealed": sealed}
    if not sealed and not args.no_seal:
        # An adk without adk.compliance.egress_guard (PyPI <= 3.8.29) cannot seal the
        # process. Running anyway would produce evidence that LOOKS air-gapped.
        ev.update(verdict="unsealed",
                  detail=f"cannot seal this process (air gap mode={mode}); "
                         "install an awdk with adk.compliance.egress_guard or pass --no-seal")
        return EXIT_NO_MODEL, ev
    corpus = Path(args.corpus)
    if not corpus.is_dir():
        ev.update(verdict="no-input", detail=f"corpus {corpus} is not a directory")
        return EXIT_NO_MODEL, ev
    docs = load_corpus(corpus, args.max_docs)
    ev["sha256_inputs"] = {d["id"]: d["sha256"] for d in docs}
    if not docs:
        ev.update(verdict="no-input", detail="corpus has no .txt/.md files")
        return EXIT_NO_MODEL, ev

    baseline = _violation_count()
    t0 = time.perf_counter()
    try:
        model = args.model or await discover_model(args.base_url, args.timeout)
        ev["model"] = model
        from adk.llm import LLMRouter
        router = LLMRouter(provider="openai", base_url=args.base_url, api_key="local",
                           model=model)
        for doc in docs:
            text, ttft = await ask(router, build_prompt(doc, args.question), model)
            if ev["ttft_s"] is None and ttft is not None:
                ev["ttft_s"] = round(ttft, 4)
            rows, problems = parse_rows(text, doc)
            ev["rows"].extend(rows)
            ev["problems"].extend(problems)
    except Exception as e:  # noqa: BLE001 - every failure becomes a verdict
        ev["wall_s"] = round(time.perf_counter() - t0, 4)
        if _is_block(e):
            ev["egress_violations"].append({"detail": str(e), "caller": _caller(e)})
            ev.update(verdict="egress-blocked", detail=str(e))
            return EXIT_FAIL, ev
        recorded = _violations_since(baseline)
        if recorded:
            ev["egress_violations"].extend(recorded)
            ev.update(verdict="egress-violation",
                      detail=f"{len(recorded)} egress violation(s) recorded (not refused)")
            return EXIT_FAIL, ev
        ev.update(verdict="no-model", detail=f"{type(e).__name__}: {e}")
        return EXIT_NO_MODEL, ev
    ev["wall_s"] = round(time.perf_counter() - t0, 4)
    recorded = _violations_since(baseline)
    if recorded:
        # Audit mode lets the dial through; a run that egressed is never "ok".
        ev["egress_violations"].extend(recorded)
        ev.update(verdict="egress-violation",
                  detail=f"{len(recorded)} egress violation(s) recorded (not refused)")
        return EXIT_FAIL, ev
    if not ev["rows"]:
        ev.update(verdict="fail", detail="the model produced no valid cited rows")
        return EXIT_FAIL, ev
    if ev["problems"]:
        ev.update(verdict="fail", detail=f"{len(ev['problems'])} invalid row(s)")
        return EXIT_FAIL, ev
    ev.update(verdict="ok", detail=f"{len(ev['rows'])} cited row(s) from {len(docs)} document(s)")
    return EXIT_OK, ev


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--corpus", required=True)
    ap.add_argument("--out", required=True, help="evidence JSON path")
    ap.add_argument("--base-url", default=os.environ.get("AITHER_LLM_BASE_URL") or DEFAULT_BASE_URL)
    ap.add_argument("--model", default=os.environ.get("AITHER_MODEL") or "")
    ap.add_argument("--question", default=DEFAULT_QUESTION)
    ap.add_argument("--max-docs", type=int, default=8)
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--no-seal", action="store_true",
                    help="do not create a strict loopback-only enforcer when none is configured")
    args = ap.parse_args(argv)
    rc, ev = asyncio.run(run(args))
    ev["exit_code"] = rc
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(ev, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"{ev['verdict']}: {ev.get('detail', '')} -> {out}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
