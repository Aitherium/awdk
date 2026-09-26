"""``tests/rubric.md``: the second signal, and its alignment with the tests.

Exactly three sections, in order::

    ## Must-do        observable outcomes; every bullet cites [test: <name>]
    ## Must-avoid     shortcuts / traps; every bullet cites [test: <name>] or [probe: <name>]
    ## Best-practice  the skill's own methodology, qualitative; cites nothing

Must-do and Must-avoid are NOT judged by a model: each bullet names the deterministic
test (or must-avoid probe, ``solution/avoid_<name>.py``) that enforces it, and the gate
checks that the named test exists. Best-practice is advisory. A judge may score a
trajectory against it later, off the reward path, as a separately reported number.

Stdlib only; 3.10-compatible.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List

__all__ = ["Rubric", "parse_rubric", "SECTIONS"]

SECTIONS = ("Must-do", "Must-avoid", "Best-practice")
_CITE = re.compile(r"\[(test|probe):\s*([A-Za-z0-9_\-]+)\]")


@dataclass
class Rubric:
    items: Dict[str, List[str]] = field(default_factory=dict)
    problems: List[str] = field(default_factory=list)

    def citations(self, section: str) -> List["tuple[str, str, str]"]:
        """``(kind, name, bullet)`` for every citation in ``section``."""
        out = []
        for bullet in self.items.get(section, []):
            for kind, name in _CITE.findall(bullet):
                out.append((kind, name, bullet))
        return out

    def best_practice(self) -> List[str]:
        return list(self.items.get("Best-practice", []))


def parse_rubric(path: Path) -> Rubric:
    text = Path(path).read_text(encoding="utf-8")
    rub = Rubric()
    order: List[str] = []
    current = None
    for raw in text.splitlines():
        line = raw.rstrip()
        m = re.match(r"^##\s+(.+?)\s*$", line)
        if m:
            current = m.group(1)
            order.append(current)
            rub.items.setdefault(current, [])
            continue
        if current and re.match(r"^\s*[-*]\s+", line):
            rub.items[current].append(re.sub(r"^\s*[-*]\s+", "", line))
        elif current and line.strip() and rub.items[current]:
            rub.items[current][-1] += " " + line.strip()  # wrapped bullet
    if tuple(order) != SECTIONS:
        rub.problems.append("rubric sections must be exactly %s in order, found %s"
                            % (list(SECTIONS), order))
    for sec in SECTIONS:
        if not rub.items.get(sec):
            rub.problems.append("rubric section %s is empty" % sec)
    for sec in ("Must-do", "Must-avoid"):
        for bullet in rub.items.get(sec, []):
            if not _CITE.search(bullet):
                rub.problems.append("%s bullet cites no [test: ...] or [probe: ...]: %s"
                                    % (sec, bullet[:80]))
    for kind, name, _b in rub.citations("Must-do"):
        if kind != "test":
            rub.problems.append("Must-do may cite only tests, not probe %s" % name)
    for bullet in rub.items.get("Best-practice", []):
        if _CITE.search(bullet):
            rub.problems.append("Best-practice is advisory and must not cite a test: %s"
                                % bullet[:80])
    return rub
