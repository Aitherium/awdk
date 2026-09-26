# vendored from h30-repl-agent@f27271775d6786b1df5dd40234005436af8081c8:agent/repl/core/memory.py -- edit only by re-vendoring (see adk/reasoning/solve/_provenance.py)
"""h30 memory and knowledge management.

(a) ``WorkingMemory``  -- the bounded chat context.  Whole turns are evicted
    oldest-first into a rolling summary; a kept message is never cut.
(b) ``Episodic``       -- every transition, queryable from model code.
(c) ``HypothesisStore`` -- per-game rule/goal hypotheses stored as CODE.  A
    hypothesis stays only while it replays perfectly over ALL history; refuted
    ones are listed so they are not retried.  Carries across levels.
(d) ``SkillLibrary``   -- per-run helper functions the model wrote that were
    used successfully; persisted through a ``MemoryBackend`` (files today) so
    later games in the same run load them.

Pure stdlib + numpy.  3.10-compatible.
"""
from __future__ import annotations

import ast
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterator, List, Optional, Set, Tuple

import numpy as np

Action = Tuple[int, int, int]  # (action_id, x, y); x = y = -1 for simple actions


def est_tokens(text: str) -> int:
    """Conservative token estimate (3.5 chars/token) for budgeting."""
    return int(len(text or "") / 3.5) + 4


# ============================================================================
# (a) working memory
# ============================================================================
@dataclass
class TurnGist:
    turn: int
    acts: int = 0
    level_ups: int = 0
    deaths: int = 0
    text: str = ""

    def line(self) -> str:
        bits = ["t%d: %d act%s" % (self.turn, self.acts, "" if self.acts == 1 else "s")]
        if self.level_ups:
            bits.append("LEVEL UP x%d" % self.level_ups)
        if self.deaths:
            bits.append("died x%d" % self.deaths)
        if self.text:
            bits.append(self.text)
        return "; ".join(bits)


class WorkingMemory:
    """Alternating (user, assistant) turn pairs under a token budget.

    Eviction removes WHOLE pairs, oldest first, and folds each into the
    rolling summary as its one-line gist.  The newest pair is always kept.
    Messages are stored as given and returned unchanged.
    """

    def __init__(self, budget_tokens: int = 1800, summary_lines: int = 10) -> None:
        self.budget_tokens = int(budget_tokens)
        self.summary_lines = int(summary_lines)
        self.pairs: List[Tuple[Dict[str, str], Dict[str, str], TurnGist]] = []
        self.summary: List[TurnGist] = []
        self.folded = TurnGist(turn=0)
        self.folded_turns = 0
        self.evicted = 0

    def add_turn(self, user: str, assistant: str, gist: TurnGist) -> None:
        self.pairs.append(({"role": "user", "content": user},
                           {"role": "assistant", "content": assistant}, gist))
        self.evict_to(self.budget_tokens)

    def tokens(self) -> int:
        return sum(est_tokens(u["content"]) + est_tokens(a["content"]) for u, a, _g in self.pairs)

    def evict_to(self, budget: int) -> int:
        n = 0
        while len(self.pairs) > 1 and self.tokens() > budget:
            _u, _a, gist = self.pairs.pop(0)
            self.summary.append(gist)
            self.evicted += 1
            n += 1
        # the summary is bounded too: its oldest lines collapse into totals
        while len(self.summary) > self.summary_lines:
            g = self.summary.pop(0)
            self.folded_turns += 1
            self.folded.acts += g.acts
            self.folded.level_ups += g.level_ups
            self.folded.deaths += g.deaths
        return n

    def messages(self) -> List[Dict[str, str]]:
        out: List[Dict[str, str]] = []
        for u, a, _g in self.pairs:
            out.append(u)
            out.append(a)
        return out

    def summary_text(self) -> str:
        lines = []
        if self.folded_turns:
            lines.append("(%d older turns: %d actions, %d level ups, %d deaths)" % (
                self.folded_turns, self.folded.acts, self.folded.level_ups, self.folded.deaths))
        lines.extend(g.line() for g in self.summary)
        return "\n".join(lines)


# ============================================================================
# (b) episodic memory
# ============================================================================
@dataclass
class Transition:
    i: int
    level: int
    action: Action
    before: np.ndarray
    after: np.ndarray
    changed: int
    level_up: bool = False
    died: bool = False
    win: Optional[np.ndarray] = None  # first animation layer on a level up
    source: str = "model"
    turn: int = 0

    def __repr__(self) -> str:
        a = self.action
        act = "A%d" % a[0] if a[0] != 6 else "A6(x=%d,y=%d)" % (a[1], a[2])
        flags = "".join([" LEVEL_UP" if self.level_up else "", " DIED" if self.died else ""])
        return "<t#%d L%d %s changed=%d%s src=%s>" % (self.i, self.level, act, self.changed, flags, self.source)


class Episodic:
    """Append-only transition store (resets are not transitions)."""

    def __init__(self) -> None:
        self.items: List[Transition] = []

    def add(self, level: int, action: Action, before: np.ndarray, after: np.ndarray,
            level_up: bool = False, died: bool = False, win: Optional[np.ndarray] = None,
            source: str = "model", turn: int = 0) -> Transition:
        b = np.asarray(before, dtype=np.int8)
        a = np.asarray(after, dtype=np.int8)
        changed = int((b != a).sum()) if b.shape == a.shape else int(a.size)
        t = Transition(len(self.items), int(level), (int(action[0]), int(action[1]), int(action[2])),
                       b, a, changed, bool(level_up), bool(died),
                       None if win is None else np.asarray(win, dtype=np.int8), source, int(turn))
        self.items.append(t)
        return t

    def __len__(self) -> int:
        return len(self.items)

    def __getitem__(self, i: Any) -> Any:
        return self.items[i]

    def __iter__(self) -> Iterator[Transition]:
        return iter(self.items)

    def __repr__(self) -> str:
        return "<history: %d transitions; history.summary() for per-action stats>" % len(self.items)

    def where(self, action: Any = None, level: Optional[int] = None, changed: Optional[bool] = None,
              level_up: Optional[bool] = None, died: Optional[bool] = None,
              source: Optional[str] = None) -> List[Transition]:
        out = []
        for t in self.items:
            if action is not None:
                if isinstance(action, (tuple, list)):
                    if tuple(int(v) for v in action) != t.action[:len(action)]:
                        continue
                elif t.action[0] != int(action):
                    continue
            if level is not None and t.level != level:
                continue
            if changed is not None and (t.changed > 0) != changed:
                continue
            if level_up is not None and t.level_up != level_up:
                continue
            if died is not None and t.died != died:
                continue
            if source is not None and t.source != source:
                continue
            out.append(t)
        return out

    def last(self, n: int = 1) -> List[Transition]:
        return self.items[-int(n):] if n > 0 else []

    def summary(self, level: Optional[int] = None) -> str:
        stats: Dict[int, List[int]] = {}
        for t in self.items:
            if level is not None and t.level != level:
                continue
            s = stats.setdefault(t.action[0], [0, 0, 0, 0])
            s[0] += 1
            s[1] += t.changed > 0
            s[2] += t.died
            s[3] += t.level_up
        if not stats:
            return "no transitions yet"
        parts = []
        for a in sorted(stats):
            n, ch, d, lu = stats[a]
            parts.append("A%d: %d tried, %d changed%s%s" % (
                a, n, ch, (", %d died" % d) if d else "", (", %d level-up" % lu) if lu else ""))
        return "; ".join(parts)


# ============================================================================
# (c) semantic memory: hypotheses as code
# ============================================================================
class ActionArg(int):
    """The action handed to a ``predict(state, action)`` rule.

    Model code writes both ``action == 1`` / ``moves.get(action)`` and
    ``action[0]`` / ``a, x, y = action``.  A plain ``(a, x, y)`` tuple made
    every int comparison silently False (measured h30: 136 of 287 rules), so
    the rule abstained or mispredicted for a reason that had nothing to do
    with the game.  This value is the action id as an ``int`` (compares and
    hashes as the id) that also indexes, unpacks and compares like the
    ``(a, x, y)`` tuple."""

    x: int
    y: int

    def __new__(cls, action: Any) -> "ActionArg":
        if isinstance(action, (tuple, list, np.ndarray)):
            vals = [int(v) for v in list(action)[:3]] + [-1, -1]
            a, x, y = vals[0], vals[1], vals[2]
        else:
            a, x, y = int(action), -1, -1
        obj = super().__new__(cls, a)
        obj.x, obj.y = x, y
        return obj

    @property
    def as_tuple(self) -> Action:
        return (int(self), self.x, self.y)

    def __getitem__(self, i: Any) -> Any:
        return self.as_tuple[i]

    def __iter__(self) -> Iterator[int]:
        return iter(self.as_tuple)

    def __len__(self) -> int:
        return 3

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, (tuple, list)):
            return self.as_tuple == tuple(other)
        return int(self) == other

    def __ne__(self, other: Any) -> bool:
        return not self.__eq__(other)

    __hash__ = int.__hash__

    def __repr__(self) -> str:
        return "Action(%d, x=%d, y=%d)" % self.as_tuple


PREDICT_FORMAT = ("return the next frame (an array; -1 = no claim on that cell), a dict of claims "
                  "{'cells': {(r, c): colour}, 'changed': bool, 'level_up': bool}, or None to abstain")


def _claims_cells(pred: Any) -> bool:
    """A prediction that commits to cell values (a frame, or ``cells``)."""
    if isinstance(pred, dict):
        return bool(pred.get("cells"))
    return isinstance(pred, (np.ndarray, list))


def score_prediction(pred: Any, before: np.ndarray, after: np.ndarray, level_up: bool = False,
                     changed_fn: Optional[Callable[[np.ndarray, np.ndarray], bool]] = None,
                     ignore: Optional[np.ndarray] = None) -> Tuple[str, str]:
    """Score one prediction against one observed transition.

    Returns ``(verdict, why)``; verdict is ``ok`` / ``wrong`` / ``abstain``
    (nothing claimed) / ``unscorable`` (a value that is not a prediction).

    Scope: a rule is judged only on what it claims.  In a predicted frame a
    cell holding -1 is no claim, and ``ignore`` (the domain's HUD / step-
    counter cells) is never judged: the harness tells the model a HUD tick
    is not a change, so it cannot also demand the tick be predicted.  An
    explicit ``cells`` claim is always judged, HUD or not.  Everything that
    IS claimed is judged strictly: one wrong cell is a wrong prediction."""
    if pred is None:
        return "abstain", ""
    if isinstance(pred, dict):
        known = [k for k in ("cells", "changed", "level_up") if k in pred]
        if not known:
            return "unscorable", "dict with keys %s; %s" % (sorted(map(str, pred))[:4], PREDICT_FORMAT)
        if "changed" in pred:
            if changed_fn is not None:
                changed = bool(changed_fn(before, after))
            else:
                changed = bool((before != after).any()) if before.shape == after.shape else True
            if bool(pred["changed"]) != changed:
                return "wrong", "predicted changed=%s, got %s" % (bool(pred["changed"]), changed)
        if "level_up" in pred and bool(pred["level_up"]) != bool(level_up):
            return "wrong", "predicted level_up=%s, got %s" % (bool(pred["level_up"]), bool(level_up))
        if "cells" in pred:
            try:
                items = list(dict(pred["cells"]).items())
            except (TypeError, ValueError):
                return "unscorable", "'cells' must map (r, c) -> colour"
            for (r, c), want in items:
                r, c = int(r), int(c)
                if not (0 <= r < after.shape[0] and 0 <= c < after.shape[1]):
                    return "wrong", "predicted cell (r%d,c%d) is off the grid" % (r, c)
                if int(after[r, c]) != int(want):
                    return "wrong", "predicted (r%d,c%d)=%d, got %d" % (r, c, int(want), int(after[r, c]))
        return "ok", "claims held"
    if isinstance(pred, (np.ndarray, list)):
        try:
            p = np.asarray(pred)
        except Exception:  # noqa: BLE001 - ragged lists
            return "unscorable", "not an array; " + PREDICT_FORMAT
        if p.ndim != 2 or p.dtype == object:
            return "unscorable", "array of shape %s; %s" % (p.shape, PREDICT_FORMAT)
        if p.shape != after.shape:
            return "wrong", "shape %s != %s" % (p.shape, after.shape)
        scope = p != -1
        if ignore is not None and ignore.shape == scope.shape:
            scope &= ~ignore
        if not scope.any():
            return "abstain", ""
        bad = np.argwhere((p != after) & scope)
        if len(bad) == 0:
            return "ok", "exact on %d claimed cells" % int(scope.sum())
        r, c = (int(v) for v in bad[0])
        return "wrong", "%d cells wrong, e.g. (r%d,c%d) predicted %d got %d" % (
            len(bad), r, c, int(p[r, c]), int(after[r, c]))
    return "unscorable", "%s; %s" % (type(pred).__name__, PREDICT_FORMAT)


def merge_predictions(preds: List[Any], shape: Tuple[int, ...]) -> Optional[np.ndarray]:
    """Several predictions of one transition as ONE frame (-1 = no claim).
    A predicted frame contributes its non -1 cells, a dict its ``cells``;
    two rules claiming different values for a cell make the merge abstain
    (None), as does nothing claimed.  ``changed`` / ``level_up`` claims are
    dropped (a frame cannot carry them)."""
    out = np.full(shape, -1, dtype=np.int16)
    any_claim = False
    for p in preds:
        if p is None:
            continue
        if isinstance(p, dict):
            try:
                items = [((int(r), int(c)), int(v)) for (r, c), v in dict(p.get("cells") or {}).items()]
            except (TypeError, ValueError):
                continue
        else:
            try:
                a = np.asarray(p)
            except Exception:  # noqa: BLE001
                continue
            if a.shape != tuple(shape) or a.dtype == object:
                continue
            rr, cc = np.nonzero(a != -1)
            items = [((int(r), int(c)), int(a[r, c])) for r, c in zip(rr.tolist(), cc.tolist())]
        for (r, c), v in items:
            if not (0 <= r < shape[0] and 0 <= c < shape[1]):
                return None
            if out[r, c] != -1 and int(out[r, c]) != v:
                return None
            out[r, c] = v
            any_claim = True
    return out if any_claim else None


@dataclass
class Hypothesis:
    name: str
    kind: str  # "predict" | "goal"
    source: str
    fn: Optional[Callable[..., Any]]
    note: str = ""
    status: str = "unsupported"  # verified | consistent | unsupported | refuted
    support: int = 0
    cell_support: int = 0  # claimed transitions that predicted CELLS (not only changed/level_up)
    positives: int = 0
    reason: str = ""
    turn: int = 0
    level: int = 0
    checked_at: int = 0  # len(history) at the last check
    refuted_at_transition: int = -1
    confidence: int = 0  # online predictions that matched (Six Pillars calibration)
    origin: str = "model"  # "model" (written by the model) | "proposer" (an induced candidate, adopted)
    cites: Tuple[str, ...] = ()  # evidence-table rows the rule cites (h31)
    must_beat: int = 0  # a model-written rule verifies only above the cited candidates' cell coverage


def _norm_source(source: str) -> str:
    """Name-independent structural fingerprint of a function's source."""
    try:
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.Lambda)):
                if isinstance(node, ast.FunctionDef):
                    node.name = "_"
                    if (node.body and isinstance(node.body[0], ast.Expr)
                            and isinstance(getattr(node.body[0], "value", None), ast.Constant)
                            and isinstance(node.body[0].value.value, str)):
                        node.body = node.body[1:] or [ast.Pass()]
        return ast.dump(tree, annotate_fields=False)
    except SyntaxError:
        return " ".join(source.split())


class HypothesisStore:
    """Rule (``predict(frame, action) -> next frame | None``) and goal
    (``goal(frame) -> bool``) hypotheses, rechecked against ALL history."""

    def __init__(self, min_support: int = 3, max_mismatch_report: int = 4,
                 ignore_fn: Optional[Callable[[np.ndarray, np.ndarray], Optional[np.ndarray]]] = None,
                 changed_fn: Optional[Callable[[np.ndarray, np.ndarray], bool]] = None,
                 skip_fn: Optional[Callable[[Any], bool]] = None) -> None:
        self.min_support = int(min_support)
        self.skip_fn = skip_fn        # (transition) -> not dynamics (the domain's soft reset / lost life)
        self.max_mismatch_report = int(max_mismatch_report)
        self.ignore_fn = ignore_fn    # (before, after) -> HUD cells never judged
        self.changed_fn = changed_fn  # (before, after) -> a significant (non-HUD) change
        self.active: Dict[str, Hypothesis] = {}
        self.refuted: List[Hypothesis] = []
        self._refuted_fp: Dict[str, str] = {}
        self.n_verified_ever: Set[str] = set()
        self.verified_origin: Dict[str, str] = {}  # name -> "model" | "proposer" (h31)
        self.n_refuted = 0
        self.n_proposed = 0
        self.claims_ok = 0
        self.claims_missed = 0

    # -- checking ---------------------------------------------------------
    def not_dynamics(self, t: Any) -> bool:
        """A transition the domain says is not a dynamics step (h31: a lost
        life that put every body back at the level start)."""
        if self.skip_fn is None:
            return False
        try:
            return bool(self.skip_fn(t))
        except Exception:  # noqa: BLE001 - perception extras never fail a check
            return False

    def ignore_mask(self, before: np.ndarray, after: np.ndarray) -> Optional[np.ndarray]:
        if self.ignore_fn is None:
            return None
        try:
            m = self.ignore_fn(before, after)
        except Exception:  # noqa: BLE001 - perception extras never fail a check
            return None
        return None if m is None else np.asarray(m, dtype=bool)

    def replay_check(self, fn: Callable[..., Any], history: Episodic, kind: str = "predict",
                     caller: Optional[Callable[..., Any]] = None, start: int = 0) -> Dict[str, Any]:
        """Replay a rule over history.  Dynamics transitions only: a level up
        (the next level's first frame) and a death (the game-over frame) are
        not what ``predict`` models.  Each prediction is judged on what it
        claims (``score_prediction``); any wrong claim fails the check."""
        call = caller or (lambda f, *a: f(*a))
        if kind == "goal":
            return self._check_goal(fn, history, call, start)
        claimed = abstained = wrong = errors = unscorable = cell_claims = 0
        first_fail: Optional[Dict[str, Any]] = None
        first_unscorable = ""
        levels: Dict[int, List[int]] = {}  # level -> [claimed, wrong]
        for t in history.items[start:]:
            if t.level_up or t.died or t.action[0] == 0 or self.not_dynamics(t):
                continue
            try:
                pred = call(fn, t.before.copy(), ActionArg(t.action))
            except Exception as exc:  # noqa: BLE001 - a raising rule is refuted, not a crash
                errors += 1
                if first_fail is None:
                    first_fail = {"t": t.i, "action": t.action, "error": "%s: %s" % (type(exc).__name__, str(exc)[:120])}
                continue
            verdict, why = score_prediction(pred, t.before, t.after, t.level_up, self.changed_fn,
                                            self.ignore_mask(t.before, t.after))
            if verdict == "abstain":
                abstained += 1
                continue
            if verdict == "unscorable":
                unscorable += 1
                first_unscorable = first_unscorable or why
                continue
            claimed += 1
            cell_claims += _claims_cells(pred)
            lv = levels.setdefault(t.level, [0, 0])
            lv[0] += 1
            if verdict == "wrong":
                wrong += 1
                lv[1] += 1
                if first_fail is None:
                    first_fail = {"t": t.i, "action": t.action, "level": t.level, "error": why}
                    if isinstance(pred, (np.ndarray, list)):
                        p = np.asarray(pred)
                        if p.shape == t.after.shape:
                            ign = self.ignore_mask(t.before, t.after)
                            m = (p != t.after) & (p != -1)
                            if ign is not None and ign.shape == m.shape:
                                m &= ~ign
                            bad = np.argwhere(m)
                            first_fail["cells_wrong"] = int(len(bad))
                            first_fail["sample(r,c,pred,true)"] = [
                                (int(r), int(c), int(p[r, c]), int(t.after[r, c]))
                                for r, c in bad[:self.max_mismatch_report]]
        if claimed == 0 and unscorable and first_fail is None:
            first_fail = {"t": -1, "error": "unscorable: " + first_unscorable}
        ok = wrong == 0 and errors == 0 and not (claimed == 0 and unscorable)
        return {"kind": "predict", "claimed": claimed, "cell_claims": cell_claims, "abstained": abstained,
                "wrong": wrong, "errors": errors, "unscorable": unscorable, "ok": ok,
                "passes": ok and cell_claims >= self.min_support, "first_fail": first_fail,
                "levels": {k: {"claimed": v[0], "wrong": v[1]} for k, v in sorted(levels.items())}}

    def _check_goal(self, fn: Callable[..., Any], history: Episodic, call: Callable[..., Any],
                    start: int) -> Dict[str, Any]:
        pos = neg = fp = fn_miss = errors = nones = 0
        first_fail: Optional[Dict[str, Any]] = None
        seen: Set[bytes] = set()
        for t in history.items[start:]:
            frames: List[Tuple[np.ndarray, bool]] = []
            if t.i == 0 or start == t.i:
                frames.append((t.before, False))
            if t.level_up:
                if t.win is not None:
                    frames.append((t.win, True))
            elif not t.died:
                frames.append((t.after, False))
            for f, is_win in frames:
                key = f.tobytes() + (b"W" if is_win else b"N")
                if key in seen:
                    continue
                seen.add(key)
                try:
                    raw = call(fn, f.copy())
                    nones += raw is None
                    v = goal_satisfied(raw)
                except Exception as exc:  # noqa: BLE001
                    errors += 1
                    if first_fail is None:
                        first_fail = {"t": t.i, "error": "%s: %s" % (type(exc).__name__, str(exc)[:120])}
                    continue
                if is_win:
                    pos += 1
                    if not v:
                        fn_miss += 1
                        if first_fail is None:
                            first_fail = {"t": t.i, "error": "returned False on a frame that WON the level"}
                else:
                    neg += 1
                    if v:
                        fp += 1
                        if first_fail is None:
                            first_fail = {"t": t.i, "error": "returned True on a non-winning frame"}
        ok = fp == 0 and fn_miss == 0 and errors == 0
        return {"kind": "goal", "wins_seen": pos, "non_wins": neg, "false_pos": fp,
                "missed_wins": fn_miss, "errors": errors, "ok": ok, "none_values": nones,
                "passes": ok and pos >= 1, "first_fail": first_fail}

    # -- lifecycle --------------------------------------------------------
    def propose(self, name: str, fn: Callable[..., Any], source: str, kind: str, history: Episodic,
                turn: int = 0, level: int = 0, note: str = "",
                caller: Optional[Callable[..., Any]] = None, origin: str = "model",
                cites: Tuple[str, ...] = (), must_beat: int = 0) -> Dict[str, Any]:
        if kind not in ("predict", "goal"):
            raise ValueError("kind must be 'predict' or 'goal'")
        self.n_proposed += 1
        fp = _norm_source(source) if source else ""
        if fp and fp in self._refuted_fp:
            return {"name": name, "status": "refuted",
                    "reason": "identical to already-refuted %r; not retried" % self._refuted_fp[fp]}
        rep = self.replay_check(fn, history, kind, caller)
        vac = self._vacuous(rep, kind)
        if vac:
            rep = dict(rep, ok=False, passes=False, first_fail={"t": -1, "error": vac})
        h = Hypothesis(name=name, kind=kind, source=source, fn=fn, note=note, turn=turn, level=level,
                       checked_at=len(history), origin=origin, cites=tuple(cites), must_beat=int(must_beat))
        self._apply(h, rep)
        if h.status == "refuted":
            self._refute(h, rep)
        else:
            self.active[name] = h
        out = {"name": name, "status": h.status, "reason": h.reason}
        out.update({k: v for k, v in rep.items() if k not in ("ok", "passes")})
        return out

    def _vacuous(self, rep: Dict[str, Any], kind: str) -> str:
        """A stub (``pass`` / ``return None`` / constant) is not a hypothesis."""
        if kind == "predict" and rep.get("claimed", 0) == 0 and rep.get("abstained", 0) >= self.min_support:
            return "vacuous: returned None on all %d transitions -- write a real rule" % rep["abstained"]
        if kind == "goal" and rep.get("none_values", 0) and rep.get("none_values", 0) >= rep.get("non_wins", 0) > 0:
            return "vacuous: returned None on every frame -- return True/False or a progress number"
        return ""

    def _apply(self, h: Hypothesis, rep: Dict[str, Any]) -> None:
        if not rep["ok"]:
            h.status = "refuted"
            ff = rep.get("first_fail") or {}
            h.refuted_at_transition = int(ff.get("t", -1))
            h.reason = _fail_text(ff)
            return
        if h.kind == "predict":
            h.support = int(rep["claimed"])
            h.cell_support = int(rep.get("cell_claims", 0))
            h.status, h.reason = self._predict_status(h)
        else:
            h.support = int(rep["non_wins"])
            h.positives = int(rep["wins_seen"])
            h.status = "verified" if rep["passes"] else "consistent"
            h.reason = "" if rep["passes"] else "no win observed yet; consistent with %d non-winning frames" % h.support
        if h.status == "verified":
            self.n_verified_ever.add(h.name)
            self.verified_origin[h.name] = h.origin

    def _predict_status(self, h: Hypothesis) -> Tuple[str, str]:
        """verified = replays with >= min_support transitions on which it
        predicted CELLS.  A rule that only ever claims ``changed`` /
        ``level_up`` cannot simulate the next state, so it stays
        ``consistent`` however long it holds."""
        if h.cell_support >= self.min_support and h.cell_support > h.must_beat:
            return "verified", ""
        if h.cell_support >= self.min_support:
            return "consistent", ("replays, but predicts cells on %d transitions and the cited candidate "
                                  "rules already cover %d: combine or generalise them to beat that"
                                  % (h.cell_support, h.must_beat))
        if h.support >= self.min_support:
            return "consistent", ("holds on %d transitions but claims only changed/level_up; "
                                  "predict cells to verify it" % h.support)
        return "unsupported", "only %d claimed transitions (< %d)" % (h.support, self.min_support)

    def _refute(self, h: Hypothesis, rep: Dict[str, Any]) -> None:
        h.status = "refuted"
        h.fn = None
        self.refuted.append(h)
        self.n_refuted += 1
        if h.source:
            self._refuted_fp[_norm_source(h.source)] = h.name
        self.active.pop(h.name, None)

    def recheck(self, history: Episodic, caller: Optional[Callable[..., Any]] = None) -> List[str]:
        """Replay every active hypothesis over the transitions added since
        its last check (the older ones already passed).  Returns the names
        refuted by the new evidence."""
        newly: List[str] = []
        for name, h in list(self.active.items()):
            if h.fn is None or h.checked_at >= len(history):
                continue
            rep = self.replay_check(h.fn, history, h.kind, caller, start=h.checked_at)
            h.checked_at = len(history)
            if not rep["ok"]:
                ff = rep.get("first_fail") or {}
                h.reason = _fail_text(ff)
                h.refuted_at_transition = int(ff.get("t", -1))
                self._refute(h, rep)
                newly.append(name)
                continue
            if h.kind == "predict":
                h.support += int(rep["claimed"])
                h.cell_support += int(rep.get("cell_claims", 0))
                if h.status != "verified":
                    h.status, h.reason = self._predict_status(h)
                    if h.status == "verified":
                        self.n_verified_ever.add(name)
                        self.verified_origin[name] = h.origin
            else:
                h.support += int(rep["non_wins"])
                h.positives += int(rep["wins_seen"])
                if h.positives >= 1 and h.status != "verified":
                    h.status, h.reason = "verified", ""
                    self.n_verified_ever.add(name)
                    self.verified_origin[name] = h.origin
        return newly

    def confirm(self, name: str) -> None:
        """An online prediction by ``name`` matched the outcome."""
        h = self.active.get(name)
        if h is not None:
            h.confidence += 1

    def refute(self, name: str, evidence: str) -> bool:
        """An online prediction by ``name`` missed: refute it with evidence."""
        h = self.active.get(name)
        if h is None:
            return False
        h.reason = "prediction missed: " + evidence
        self._refute(h, {})
        return True

    def record_claim(self, text: str, evidence: str, ok: bool, turn: int = 0, level: int = 0) -> None:
        """An inline prediction (``act(..., expect=...)`` without a named
        hypothesis).  A miss is kept as a refuted claim, with its evidence,
        so the digest shows it and it is not assumed again."""
        if ok:
            self.claims_ok += 1
            return
        h = Hypothesis(name="claim@t%d" % turn, kind="claim", source="", fn=None,
                       note=text[:100], status="refuted", reason=evidence[:160], turn=turn, level=level)
        self.refuted.append(h)
        self.n_refuted += 1
        self.claims_missed += 1

    def verified(self) -> Dict[str, Hypothesis]:
        return {k: h for k, h in self.active.items() if h.status == "verified"}

    def digest(self, max_items: int = 6) -> str:
        lines: List[str] = []
        order = {"verified": 0, "consistent": 1, "unsupported": 2}
        act = sorted(self.active.values(), key=lambda h: (order.get(h.status, 3), -h.support))
        for h in act[:max_items]:
            extra = " wins=%d" % h.positives if h.kind == "goal" else ""
            conf = " conf=%d" % h.confidence if h.confidence else ""
            src = " candidate" if h.origin == "proposer" else ""
            cells = " cells=%d" % h.cell_support if h.kind == "predict" else ""
            lines.append("  %s %s [%s%s, support=%d%s%s%s]%s" % (
                h.status.upper(), h.name, h.kind, src, h.support, cells, extra, conf,
                (" -- " + h.note[:80]) if h.note else ""))
            if h.status != "verified" and h.reason:
                lines.append("    why not verified: " + h.reason[:140])
        for h in self.refuted[-max_items:]:
            cites = (" cites " + ",".join(h.cites)) if h.cites else ""
            lines.append("  REFUTED (do not retry) %s [%s%s]: %s" % (h.name, h.kind, cites, h.reason[:110]))
        return "\n".join(lines) if lines else "  (none yet)"

    def stats(self) -> Dict[str, Any]:
        by_origin: Dict[str, int] = {}
        for o in self.verified_origin.values():
            by_origin[o] = by_origin.get(o, 0) + 1
        return {"proposed": self.n_proposed, "verified_ever": len(self.n_verified_ever),
                "verified_by_origin": by_origin,
                "verified_now": len(self.verified()), "refuted": self.n_refuted,
                "active": len(self.active), "claims_ok": self.claims_ok,
                "claims_missed": self.claims_missed}


def goal_score(v: Any) -> float:
    """A goal function may return a bool or a progress number in [0, 1]."""
    if isinstance(v, (bool, np.bool_)):
        return 1.0 if v else 0.0
    try:
        return max(0.0, min(1.0, float(v)))
    except (TypeError, ValueError):
        return 1.0 if v else 0.0


def goal_satisfied(v: Any) -> bool:
    return goal_score(v) >= 1.0


def _fail_text(ff: Dict[str, Any]) -> str:
    if not ff:
        return "failed replay"
    bits = ["at t#%s" % ff.get("t")]
    if "action" in ff:
        bits.append("action %s" % (ff["action"],))
    if "cells_wrong" in ff:
        bits.append("%d cells wrong e.g. %s" % (ff["cells_wrong"], ff.get("sample(r,c,pred,true)", [])[:2]))
    if "error" in ff and "cells_wrong" not in ff:
        bits.append(str(ff["error"]))
    return " ".join(bits)


# ============================================================================
# (d) procedural memory: the skill library
# ============================================================================
def top_level_defs(code: str) -> Dict[str, Dict[str, str]]:
    """``{name: {source, doc, sig}}`` for the top-level functions in ``code``."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return {}
    out: Dict[str, Dict[str, str]] = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            src = ast.get_source_segment(code, node) or ""
            if not src:
                continue
            doc = (ast.get_docstring(node) or "").strip().splitlines()
            args = [a.arg for a in node.args.args]
            out[node.name] = {"source": src, "doc": doc[0][:120] if doc else "",
                              "sig": "%s(%s)" % (node.name, ", ".join(args))}
    return out


def called_names(code: str) -> Set[str]:
    """Every bare name called anywhere in ``code`` (``f(...)``)."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return set()
    return {n.func.id for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}


def merge_skill_maps(disk: Optional[Dict[str, Any]], mine: Dict[str, Any],
                     max_skills: int = 40) -> Dict[str, Any]:
    """Merge two skill maps: newest version wins, counts are max'ed, game
    sets are unioned; the most successful ``max_skills`` survive."""
    merged: Dict[str, Any] = dict(disk or {})
    for name, s in mine.items():
        d = merged.get(name)
        if d is None or int(s.get("version", 1)) >= int(d.get("version", 1)):
            m = dict(s)
            if d is not None:
                for k in ("successes", "uses", "reuse_successes"):
                    m[k] = max(int(s.get(k, 0)), int(d.get(k, 0)))
                for k in ("games", "reused_in"):
                    m[k] = sorted(set(s.get(k, [])) | set(d.get(k, [])))
            merged[name] = m
    if len(merged) > max_skills:
        keep = sorted(merged.items(), key=lambda kv: (-int(kv[1].get("successes", 0)),
                                                      -float(kv[1].get("updated", 0))))
        merged = dict(keep[:max_skills])
    return merged


class SkillLibrary:
    """Helpers that worked, persisted through a ``MemoryBackend``
    (namespace ``procedural``, key ``skills``) so later games load them.
    Saves merge with what the backend holds (another game may have saved
    meanwhile)."""

    NAMESPACE = "procedural"
    KEY = "skills"

    def __init__(self, backend: Any = None, max_skills: int = 40) -> None:
        self.backend = backend
        self.max_skills = int(max_skills)
        self.skills: Dict[str, Dict[str, Any]] = {}
        if backend is not None:
            got = backend.get(self.NAMESPACE, self.KEY, {}) or {}
            self.skills = {k: v for k, v in got.items() if isinstance(v, dict)}
        self.loaded_at_start: Set[str] = set(self.skills)

    def save(self) -> None:
        if self.backend is None:
            return
        mine = self.skills
        self.skills = self.backend.update(self.NAMESPACE, self.KEY,
                                          lambda disk: merge_skill_maps(disk, mine, self.max_skills))

    def record_success(self, name: str, source: str, doc: str, sig: str, game: str) -> bool:
        """A helper defined in a successful turn. Returns True if new."""
        s = self.skills.get(name)
        now = time.time()
        if s is None:
            self.skills[name] = {"name": name, "source": source, "doc": doc, "sig": sig,
                                 "created_game": game, "version": 1, "successes": 1, "uses": 0,
                                 "reuse_successes": 0, "games": [game], "reused_in": [],
                                 "updated": now}
            return True
        if s.get("source") != source:
            s.update({"source": source, "doc": doc or s.get("doc", ""), "sig": sig,
                      "version": int(s.get("version", 1)) + 1})
        s["successes"] = int(s.get("successes", 0)) + 1
        s["games"] = sorted(set(s.get("games", [])) | {game})
        s["updated"] = now
        return False

    def record_use(self, name: str, game: str, ok: bool) -> bool:
        """A library helper called (not redefined) by a turn. Returns True
        when it is a cross-game reuse (created by an earlier game)."""
        s = self.skills.get(name)
        if s is None:
            return False
        s["uses"] = int(s.get("uses", 0)) + 1
        cross = s.get("created_game") != game
        if cross:
            s["reused_in"] = sorted(set(s.get("reused_in", [])) | {game})
            if ok:
                s["reuse_successes"] = int(s.get("reuse_successes", 0)) + 1
        if ok:
            s["successes"] = int(s.get("successes", 0)) + 1
            s["games"] = sorted(set(s.get("games", [])) | {game})
        s["updated"] = time.time()
        return cross

    def install(self, define: Callable[[str, str], Optional[str]]) -> List[str]:
        """Define every skill in a namespace via ``define(source, filename)``."""
        ok = []
        for name, s in sorted(self.skills.items()):
            err = define(str(s.get("source", "")), "<h30-skill:%s>" % name)
            if err is None:
                ok.append(name)
        return ok

    def digest(self, max_n: int = 10) -> str:
        items = sorted(self.skills.values(), key=lambda s: -int(s.get("successes", 0)))[:max_n]
        if not items:
            return "  (none yet)"
        return "\n".join("  %s -- %s [ok %d, games %d]" % (
            s.get("sig", s["name"]), (s.get("doc") or "")[:90], int(s.get("successes", 0)),
            len(s.get("games", []))) for s in items)

    def stats(self) -> Dict[str, Any]:
        return {"skills": len(self.skills), "loaded_at_start": len(self.loaded_at_start)}


__all__ = ["WorkingMemory", "TurnGist", "Episodic", "Transition", "HypothesisStore", "Hypothesis",
           "ActionArg", "score_prediction", "PREDICT_FORMAT", "merge_predictions",
           "SkillLibrary", "merge_skill_maps", "goal_score", "goal_satisfied", "top_level_defs", "called_names", "est_tokens"]
