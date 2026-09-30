"""Aither Learn tools for the home agent: a parent asks about their kids' learning.

Four read-mostly tools over the Genesis family-tutor router
(``/api/v1/tutor/family/*``), called with the GUARDIAN's bearer so the router's
guardian scoping applies exactly as it does in the parent console::

    tutor_learners()                        the guardian's own learners
    tutor_report(lid)                       one learner's weekly report, as parent text
    tutor_assign(lid, skill_id, note='')    queue a skill for the next quest
    tutor_set_focus(lid, skills, note='')   this week's focus skills + a coach note

``lid`` may be the learner id OR the child's name ("Alexander"), matched on the
guardian's own roster; ``skill_id`` may be a skill id OR a parent's phrase
("short vowels"), mapped by :data:`SKILL_ALIASES` (``tutor_set_focus`` resolves the
child and every focus skill the same way). A small home model can then act
on "have Athena practice short vowels" without first learning the id scheme.

Rules these tools keep (family-tutor plan, WP5):

* No local grading and no answer keys: correctness is server-only, so nothing
  here ever sees or computes an answer.
* No tenant, user or guardian id is an argument -- the bearer is the identity.
* Every tool returns a JSON string; an HTTP or network failure is a JSON
  ``{"error": ...}``, never an exception into the agent loop.
* No tool name starts with ``file_`` or ``shell`` (the approval plane treats
  those prefixes as dangerous, and nothing here touches the disk or a shell).
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote

PREFIX = "/api/v1/tutor/family"
_TIMEOUT = 30.0
_MAX_NOTE = 140
_MAX_COACH_NOTE = 280
_MAX_FOCUS_SKILLS = 6
#: A value that looks like a child's name rather than a learner id.
_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z .'-]{0,63}$")
#: Report keys never passed to the model: correctness is server-only.
_HIDDEN_KEY = re.compile(r"answer|^correct|^expected$|^solution", re.IGNORECASE)
#: A parent's words -> the skill-graph id (AitherOS/config/tutor/skill_graph.yaml). A
#: test asserts every target still exists in the graph when the monorepo is present.
SKILL_ALIASES: Dict[str, str] = {
    "rhymes": "read.pa.rhyme", "rhyming": "read.pa.rhyme",
    "syllables": "read.pa.syllable",
    "first sounds": "read.pa.isolate_initial", "beginning sounds": "read.pa.isolate_initial",
    "blending": "read.pa.blend_cvc", "blending sounds": "read.pa.blend_cvc",
    "segmenting": "read.pa.segment_cvc",
    "letter sounds": "read.phx.letter_sounds", "phonics": "read.phx.letter_sounds",
    "short vowels": "read.phx.cvc", "cvc": "read.phx.cvc", "cvc words": "read.phx.cvc",
    "digraphs": "read.phx.digraphs", "sh ch th": "read.phx.digraphs",
    "blends": "read.phx.blends", "consonant blends": "read.phx.blends",
    "long vowels": "read.phx.vce", "magic e": "read.phx.vce", "silent e": "read.phx.vce",
    "vowel teams": "read.phx.vowel_teams_1",
    "bossy r": "read.phx.r_controlled", "r controlled": "read.phx.r_controlled",
    "sight words": "read.sw.dolch_primer", "heart words": "read.sw.dolch_primer",
    "reading fluency": "read.flu.decodable_sentence", "fluency": "read.flu.decodable_sentence",
    "retelling": "read.comp.retell", "comprehension": "read.comp.wh_questions",
    "counting": "math.count_to_120", "count to 120": "math.count_to_120",
    "make ten": "math.make_ten_strategy", "number bonds": "math.number_bonds_10",
    "addition": "math.add_within_20", "adding": "math.add_within_20",
    "subtraction": "math.sub_within_20", "subtracting": "math.sub_within_20",
    "doubles": "math.doubles_near_doubles",
    "math facts": "math.fluency_20", "fact fluency": "math.fluency_20",
    "place value": "math.place_value_2digit", "tens and ones": "math.place_value_2digit",
    "word problems": "math.word_problems_20",
    "telling time": "math.time_hour_half", "time": "math.time_hour_half",
    "money": "math.money", "coins": "math.money",
    "skip counting": "math.skip_count", "odd and even": "math.odd_even",
    "measuring": "math.measure_nonstandard",
}
_FILLER = re.compile(r"\b(practi[cs]e|practicing|work on|some|more|the|her|his|with)\b")


def resolve_skill(value: Any) -> str:
    """A skill id as given, or the id a parent's phrase maps to ("short vowels")."""
    text = str(value or "").strip()
    if "." in text and " " not in text:
        return text
    key = " ".join(_FILLER.sub(" ", re.sub(r"[^a-z ]+", " ", text.lower())).split())
    if key in SKILL_ALIASES:
        return SKILL_ALIASES[key]
    singular = " ".join(w[:-1] if w.endswith("s") else w for w in key.split())
    for alias, sid in SKILL_ALIASES.items():
        if singular == " ".join(w[:-1] if w.endswith("s") else w for w in alias.split()):
            return sid
    return text


def focus_skills(value: Any) -> List[str]:
    """Skill ids from a focus list: ids or parent phrases, comma separated.

    A comma-free chunk is one phrase when it names a known skill ("short vowels"),
    otherwise each word is its own skill ("math.make10 math.add10", "rhymes blending").
    Order is kept and duplicates dropped.
    """
    if isinstance(value, (list, tuple)):
        chunks = [str(v) for v in value]
    else:
        chunks = str(value or "").split(",")
    out: List[str] = []
    for chunk in (c.strip() for c in chunks):
        if not chunk:
            continue
        whole = resolve_skill(chunk)
        parts = [whole] if (whole != chunk or " " not in chunk) else [
            resolve_skill(w) for w in chunk.split()]
        out.extend(p for p in parts if p)
    return list(dict.fromkeys(out))


def _strip_answers(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _strip_answers(v) for k, v in value.items()
                if not _HIDDEN_KEY.search(str(k))}
    if isinstance(value, list):
        return [_strip_answers(v) for v in value]
    return value


def parent_report(report: Dict[str, Any], name: str = "") -> Dict[str, Any]:
    """The Genesis weekly report as parent-safe text plus the few facts behind it.

    Warm and factual: minutes, sessions, what is secure, what is next, the
    router's own gentle observations and notes. Never an answer key, never a
    per-item right/wrong list, never a mastery score to rank a child by.
    """
    who = name or "Your child"
    minutes = report.get("minutes") or 0
    sessions = int(report.get("sessions") or 0)
    raw_skills = report.get("skills")
    skills: Dict[str, Any] = raw_skills if isinstance(raw_skills, dict) else {}

    def _title(sid: Any) -> str:
        row = skills.get(sid)
        return str((row.get("kid_title") if isinstance(row, dict) else None) or sid)

    secure = [_title(k) for k, v in sorted(skills.items())
              if isinstance(v, dict) and str(v.get("state", "")).lower()
              in ("secure", "secured", "mastered")]
    working = [_title(s) for s in (report.get("edge") or [])][:3]
    lines: List[str] = []
    if sessions:
        lines.append(f"{who} did {sessions} learning session{'s' if sessions != 1 else ''} "
                     f"this week ({minutes} minutes).")
    else:
        lines.append(f"{who} has no sessions yet this week. That is fine: the next quest "
                     "picks up right where they left off.")
    lines.extend(str(o) for o in (report.get("observations") or []))
    if working:
        lines.append("Up next: " + "; ".join(working))
    lines.extend(str(n) for n in (report.get("notes") or []))
    return {
        "learner": name, "week": report.get("week", ""), "summary": " ".join(lines),
        "minutes": minutes, "sessions": sessions, "secure_skills": secure,
        "working_on": working,
    }


def _seg(value: Any) -> str:
    """One URL path segment -- a learner id can never walk the path."""
    return quote(str(value).strip(), safe="")


def _verify() -> Any:
    try:
        from adk._tls import tls_verify
    except ImportError:  # pragma: no cover - older adk without the TLS helper
        return True
    return tls_verify()


def build_tutor_tools(base_url: str, token: str, *,
                      client: Optional[Any] = None) -> List[Callable[..., str]]:
    """The tutor tools bound to one Genesis URL and one guardian bearer.

    ``client`` is an optional ``httpx.Client``-like object (tests pass one with a
    mock transport); by default each call opens a short-lived client.
    """
    root = (base_url or "").rstrip("/") + PREFIX

    def _call(method: str, path: str, body: Optional[Dict[str, Any]] = None,
              params: Optional[Dict[str, Any]] = None) -> str:
        if not token:
            return json.dumps({"error": "not signed in: the tutor tools need the guardian's "
                                        "bearer"})
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        try:
            if client is not None:
                resp = client.request(method, root + path, json=body, params=params,
                                      headers=headers)
            else:
                import httpx

                with httpx.Client(timeout=_TIMEOUT, verify=_verify()) as c:
                    resp = c.request(method, root + path, json=body, params=params,
                                     headers=headers)
        except Exception as exc:  # noqa: BLE001 - tools return error JSON, never raise
            return json.dumps({"error": f"tutor service unreachable: {type(exc).__name__}"})
        try:
            data = resp.json()
        except ValueError:
            data = {"detail": (resp.text or "")[:300]}
        if 200 <= resp.status_code < 300:
            return json.dumps(data, default=str)
        detail = data.get("detail", data) if isinstance(data, dict) else data
        if resp.status_code == 401:
            msg = "not signed in"
        elif resp.status_code == 404:
            msg = "no such learner for this guardian"
        else:
            msg = detail if isinstance(detail, str) else json.dumps(detail, default=str)
        return json.dumps({"error": msg, "status": resp.status_code})

    def _learners() -> Tuple[Optional[List[Dict[str, Any]]], str]:
        raw = _call("GET", "/learners")
        data = json.loads(raw)
        if isinstance(data, dict) and "error" in data:
            return None, raw
        return (data if isinstance(data, list) else []), raw

    def _resolve(lid: Any) -> Tuple[str, str, str]:
        """(lid, child's name, error JSON). A name is matched on the guardian's roster."""
        text = str(lid or "").strip()
        if not text or not _NAME_RE.match(text) or text.lower().startswith("lrn_"):
            return text, "", ""
        rows, raw = _learners()
        if rows is None:
            return "", "", raw
        want = text.lower()

        def _alias(row: Dict[str, Any]) -> str:
            return str(row.get("alias", "")).strip().lower()

        exact = [r for r in rows if _alias(r) == want]
        first = [r for r in rows if _alias(r).split(" ")[:1] == want.split(" ")[:1]]
        hits = exact or first
        if len(hits) == 1:
            return str(hits[0].get("lid", "")), str(hits[0].get("alias", "")), ""
        names = ", ".join(str(r.get("alias", "")) for r in rows) or "none enrolled"
        why = "more than one child matches" if hits else "no child by that name"
        return "", "", json.dumps({"error": f"{why} ({text}); your children: {names}"})

    def tutor_learners() -> str:
        """List your children enrolled in Aither Learn (name, grade, learner id).

        Call this first when you do not know a child's learner id.
        """
        return _call("GET", "/learners")

    def tutor_report(lid: str) -> str:
        """How one child did this week, in plain words: time, sessions, what is secure,
        what is next, and gentle observations. Use for "how did <child> do this week?".

        lid: the child's name (e.g. Alexander) or learner id from tutor_learners
        """
        if not str(lid or "").strip():
            return json.dumps({"error": "lid is required"})
        rid, name, err = _resolve(lid)
        if err:
            return err
        raw = _call("GET", f"/learners/{_seg(rid)}/report")
        data = json.loads(raw)
        if not isinstance(data, dict) or "error" in data:
            return raw
        return json.dumps(parent_report(_strip_answers(data), name), default=str)

    def tutor_assign(lid: str, skill_id: str, note: str = "") -> str:
        """Ask the tutor to include a skill in the child's next quest (asks the owner first).

        lid: the child's name (e.g. Athena) or learner id from tutor_learners
        skill_id: the skill, as an id (read.phx.cvc) or plain words (short vowels)
        note: optional short note for the child's plan (140 characters max)
        """
        if not str(lid or "").strip() or not str(skill_id or "").strip():
            return json.dumps({"error": "lid and skill_id are required"})
        text = str(note or "").strip()
        if len(text) > _MAX_NOTE:
            return json.dumps({"error": f"note is limited to {_MAX_NOTE} characters"})
        rid, _name, err = _resolve(lid)
        if err:
            return err
        body: Dict[str, Any] = {"skill_id": resolve_skill(skill_id)}
        if text:
            body["note"] = text
        return _call("POST", f"/learners/{_seg(rid)}/assign", body=body)

    def tutor_set_focus(lid: str, skills: str, note: str = "") -> str:
        """Set this week's learning focus for one child, plus an optional coach note
        (asks the owner first).

        About half of the new-learning practice comes from the focus skills whose
        building blocks the child already has; reviews are unchanged. The coach note
        only flavours the tone of hints (e.g. "loves dinosaurs, keep it playful"); it
        can never change answers, grading or safety rules. Links and contact details
        are removed. An empty skills list and empty note clear the focus.

        lid: the child's name (e.g. Athena) or learner id from tutor_learners
        skills: up to 6 skills separated by commas, as ids (read.pa.rhyme) or plain
            words (rhymes, short vowels)
        note: optional coach note for the tutor's tone (280 characters max)
        """
        if not str(lid or "").strip():
            return json.dumps({"error": "lid is required"})
        ids = focus_skills(skills)
        if len(ids) > _MAX_FOCUS_SKILLS:
            return json.dumps({"error": f"at most {_MAX_FOCUS_SKILLS} focus skills"})
        text = str(note or "").strip()
        if len(text) > _MAX_COACH_NOTE:
            return json.dumps({"error": f"note is limited to {_MAX_COACH_NOTE} characters"})
        rid, _name, err = _resolve(lid)
        if err:
            return err
        return _call("PUT", f"/learners/{_seg(rid)}/focus", body={"skills": ids, "note": text})

    return [tutor_learners, tutor_report, tutor_assign, tutor_set_focus]
