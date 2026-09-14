"""Remember what a placement ACTUALLY did, and correct the prediction with it.

`inference_placement_solver` predicts decode throughput from an analytic cost
model: bytes read per tier divided by that tier's bandwidth, plus hop latency.
That model is sound and it is never exactly right -- real throughput also
carries kernel efficiency, driver behaviour, thermal state, what else is on the
box, and the difference between a tier's rated bandwidth and its achieved
bandwidth.

The fix is not a better formula. It is memory.

WHY A LOOKUP AND NOT A LEARNED MODEL, measured rather than assumed. On this
platform's own transition data (20,000 records), predicting the next outcome:

    FROZEN per-action majority                 0.5522
    FROZEN per-(state, action) majority        0.5567
    ONLINE per-action majority                 0.9623
    ONLINE last-outcome(state, action)         0.9776

A self-updating lookup beats the frozen predictors by more than forty points,
and a separately-trained transition model scored 0.9357 against a two-line
lookup's 0.9720 on the same question -- winning only on the ~1% of rows whose
(state, action) pair had never been seen. Placement has exactly that shape:
the same node pair, model and split recur constantly, and a genuinely novel
combination is rare.

So: remember every observation, answer from memory when the situation has been
seen, and fall back to the analytic model when it has not. The analytic model is
the cold-start path, not the authority.

WHAT THIS DELIBERATELY DOES NOT DO: it never invents a measurement. An
unobserved key returns None, and the caller keeps the solver's prediction. A
memory that interpolates between neighbours would produce a confident number
with no observation behind it, which is worse than the analytic estimate it
replaced -- at least that one is honest about being a model.
"""

from __future__ import annotations

import json
import math
import os
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 1

#: Observations older than this stop counting. Hardware, drivers and the rest of
#: the fleet all move; a six-month-old throughput figure describes a machine that
#: no longer exists. Expiry is by AGE, not by count, because a rarely-used node
#: must not be evicted by a busy one.
DEFAULT_MAX_AGE_S = 30 * 24 * 3600

#: Below this many observations a key is reported but flagged low-confidence. One
#: sample can land during a thermal throttle or a neighbouring job.
MIN_CONFIDENT_SAMPLES = 3


@dataclass(frozen=True)
class PlacementKey:
    """What makes two placements 'the same situation'.

    Node IDENTITY is part of the key, not just node capability. Two boxes with
    the same rated bandwidth routinely differ in achieved throughput, and
    collapsing them would average away exactly the signal this memory exists to
    capture.
    """

    model_id: str
    #: Node ids in the placement, order-independent.
    nodes: tuple[str, ...]
    #: Which tier each tensor class landed on, e.g. "routed_experts=ram".
    tier_assignment: tuple[str, ...]

    @staticmethod
    def build(
        model_id: str,
        nodes: Iterable[str],
        tier_assignment: dict[str, str] | None = None,
    ) -> "PlacementKey":
        return PlacementKey(
            model_id=model_id,
            nodes=tuple(sorted(str(n) for n in nodes)),
            tier_assignment=tuple(
                f"{k}={v}" for k, v in sorted((tier_assignment or {}).items())
            ),
        )

    def as_str(self) -> str:
        return json.dumps(
            {"m": self.model_id, "n": list(self.nodes), "t": list(self.tier_assignment)},
            sort_keys=True,
        )


@dataclass
class Observation:
    """One measured run. `predicted_tok_s` is kept so drift is auditable."""

    tok_s: float
    at: float
    predicted_tok_s: float | None = None
    note: str = ""


@dataclass
class Recall:
    """What memory knows about a situation."""

    tok_s: float
    samples: int
    confident: bool
    newest_age_s: float
    #: Observed divided by predicted, when predictions were recorded. A number
    #: far from 1.0 means the analytic model is systematically off for this
    #: shape, which is worth surfacing rather than silently correcting away.
    calibration: float | None = None


class PlacementMemory:
    """Durable store of observed placement throughput.

    Persistence is a JSON file written atomically. Deliberately not a database:
    this runs on a stranger's laptop as part of a pip-installed package, and a
    dependency that must be installed and running is a dependency that will not
    be, at which point the memory silently does nothing.
    """

    def __init__(
        self,
        path: str | os.PathLike[str] | None = None,
        max_age_s: float = DEFAULT_MAX_AGE_S,
    ) -> None:
        self.path = Path(path) if path else _default_path()
        self.max_age_s = max_age_s
        self._data: dict[str, list[Observation]] = {}
        self._loaded = False

    # -- persistence ------------------------------------------------------

    def load(self) -> "PlacementMemory":
        """Read the store. A corrupt or unreadable file starts EMPTY and says so.

        It must never raise: this is an optimisation layer, and taking down a
        placement decision because a cache file was truncated by a power cut
        would make the memory a liability rather than a help.
        """
        self._loaded = True
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return self
        except (OSError, ValueError) as exc:
            print(
                f"placement memory: ignoring unreadable store at {self.path} ({exc}); "
                "starting empty",
                file=sys.stderr,
            )
            return self
        if not isinstance(raw, dict) or raw.get("schema") != SCHEMA_VERSION:
            # A schema we do not understand is discarded rather than guessed at.
            return self
        entries = raw.get("entries")
        if not isinstance(entries, dict):
            return self
        for key, obs_list in entries.items():
            if not isinstance(obs_list, list):
                continue
            kept: list[Observation] = []
            for o in obs_list:
                if not isinstance(o, dict):
                    continue
                try:
                    kept.append(
                        Observation(
                            tok_s=float(o["tok_s"]),
                            at=float(o["at"]),
                            predicted_tok_s=(
                                float(o["predicted_tok_s"])
                                if o.get("predicted_tok_s") is not None
                                else None
                            ),
                            note=str(o.get("note", "")),
                        )
                    )
                except (KeyError, TypeError, ValueError):
                    continue
            if kept:
                self._data[key] = kept
        return self

    def save(self) -> None:
        """Atomic write. A half-written store is a corrupt store on next boot."""
        payload = {
            "schema": SCHEMA_VERSION,
            "entries": {k: [asdict(o) for o in v] for k, v in self._data.items()},
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=1, sort_keys=True)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # -- the two operations that matter -----------------------------------

    def record(
        self,
        key: PlacementKey,
        tok_s: float,
        predicted_tok_s: float | None = None,
        note: str = "",
        now: float | None = None,
    ) -> None:
        """Remember a real measurement.

        A non-positive or non-finite rate is REFUSED rather than stored. A zero
        would drag every average toward zero and read as a real slow run, and a
        failed benchmark reporting 0 tok/s is the most likely way one arrives.
        """
        if not math.isfinite(tok_s) or tok_s <= 0:
            raise ValueError(
                f"refusing to record a non-positive throughput ({tok_s!r}); a failed "
                "measurement must not become a remembered slow run"
            )
        if not self._loaded:
            self.load()
        self._data.setdefault(key.as_str(), []).append(
            Observation(
                tok_s=float(tok_s),
                at=float(now if now is not None else time.time()),
                predicted_tok_s=predicted_tok_s,
                note=note,
            )
        )

    def recall(self, key: PlacementKey, now: float | None = None) -> Recall | None:
        """What we measured for this exact situation, or None.

        None means "never observed" and the caller must keep the analytic
        prediction. It does NOT mean slow, and it must never be coerced to a
        number.
        """
        if not self._loaded:
            self.load()
        obs = self._data.get(key.as_str())
        if not obs:
            return None
        t = now if now is not None else time.time()
        fresh = [o for o in obs if (t - o.at) <= self.max_age_s]
        if not fresh:
            return None

        # Newest-weighted, because the most recent run describes the machine as
        # it is now. A flat mean lets a months-old figure outvote this morning's.
        fresh.sort(key=lambda o: o.at)
        weights = [2.0 ** i for i in range(len(fresh))]
        total_w = sum(weights)
        tok_s = sum(o.tok_s * w for o, w in zip(fresh, weights)) / total_w

        with_pred = [o for o in fresh if o.predicted_tok_s and o.predicted_tok_s > 0]
        calibration = (
            sum(o.tok_s / float(o.predicted_tok_s) for o in with_pred) / len(with_pred)
            if with_pred
            else None
        )

        return Recall(
            tok_s=tok_s,
            samples=len(fresh),
            confident=len(fresh) >= MIN_CONFIDENT_SAMPLES,
            newest_age_s=t - fresh[-1].at,
            calibration=calibration,
        )

    def prune(self, now: float | None = None) -> int:
        """Drop expired observations. Returns how many were removed."""
        if not self._loaded:
            self.load()
        t = now if now is not None else time.time()
        removed = 0
        for key in list(self._data):
            keep = [o for o in self._data[key] if (t - o.at) <= self.max_age_s]
            removed += len(self._data[key]) - len(keep)
            if keep:
                self._data[key] = keep
            else:
                del self._data[key]
        return removed

    def stats(self) -> dict[str, Any]:
        if not self._loaded:
            self.load()
        return {
            "keys": len(self._data),
            "observations": sum(len(v) for v in self._data.values()),
            "path": str(self.path),
        }


def corrected_throughput(
    memory: PlacementMemory,
    key: PlacementKey,
    predicted_tok_s: float,
    now: float | None = None,
) -> tuple[float, str]:
    """The number to plan with, and one line saying where it came from.

    Returning the provenance alongside the value is the point. A caller that
    cannot tell a measurement from an estimate will present both with the same
    confidence, and the estimate is the one that gets someone's afternoon.
    """
    r = memory.recall(key, now=now)
    if r is None:
        return predicted_tok_s, "analytic estimate (never measured on these nodes)"
    if not r.confident:
        return (
            r.tok_s,
            f"measured, but only {r.samples} run(s) -- treat as provisional "
            f"(analytic estimate was {predicted_tok_s:.1f} tok/s)",
        )
    drift = (
        f", analytic model runs {r.calibration:.2f}x" if r.calibration is not None else ""
    )
    return r.tok_s, f"measured over {r.samples} runs{drift}"


def _default_path() -> Path:
    return Path(os.path.expanduser("~")) / ".aither" / "placement-memory.json"


# ---------------------------------------------------------------------------
# self-test
# ---------------------------------------------------------------------------


def _self_test() -> int:
    failures: list[str] = []
    now = 1_000_000.0

    with tempfile.TemporaryDirectory() as td:
        store = Path(td) / "mem.json"
        k = PlacementKey.build("example/moe-model", ["spark", "5090"],
                               {"routed_experts": "ram", "attention": "vram"})

        m = PlacementMemory(store).load()

        # Never observed -> None, and the caller keeps its estimate.
        if m.recall(k, now=now) is not None:
            failures.append("an unobserved key returned something")
        val, why = corrected_throughput(m, k, 12.0, now=now)
        if val != 12.0 or "analytic" not in why:
            failures.append("an unobserved key did not fall back to the estimate")

        # A failed benchmark must not become a remembered slow run.
        for bad in (0.0, -1.0, float("nan"), float("inf")):
            try:
                m.record(k, bad, now=now)
            except ValueError:
                pass
            else:
                failures.append(f"recorded a non-positive throughput {bad!r}")

        m.record(k, 18.0, predicted_tok_s=12.0, now=now)
        r = m.recall(k, now=now)
        if r is None or abs(r.tok_s - 18.0) > 1e-9:
            failures.append("a single observation was not recalled")
        if r and r.confident:
            failures.append("one sample was reported as confident")

        m.record(k, 19.0, predicted_tok_s=12.0, now=now + 10)
        m.record(k, 20.0, predicted_tok_s=12.0, now=now + 20)
        r = m.recall(k, now=now + 20)
        if r is None or not r.confident:
            failures.append("three samples were not reported as confident")
        # Newest-weighted: must sit above the flat mean of 19.0.
        if r and not (19.0 < r.tok_s <= 20.0):
            failures.append(f"recall {r.tok_s if r else None} is not newest-weighted")
        if r and (r.calibration is None or r.calibration < 1.4):
            failures.append("calibration did not detect the analytic model under-predicting")

        # A different tier assignment is a DIFFERENT situation.
        k2 = PlacementKey.build("example/moe-model", ["spark", "5090"],
                                {"routed_experts": "vram", "attention": "vram"})
        if m.recall(k2, now=now + 20) is not None:
            failures.append("a different tier assignment collided with the first key")

        # Node order must not matter.
        k3 = PlacementKey.build("example/moe-model", ["5090", "spark"],
                                {"attention": "vram", "routed_experts": "ram"})
        if m.recall(k3, now=now + 20) is None:
            failures.append("node ORDER changed the key")

        # Round trip.
        m.save()
        again = PlacementMemory(store).load()
        if again.recall(k, now=now + 20) is None:
            failures.append("observations did not survive a save/load")

        # Expiry by age.
        old = again.recall(k, now=now + DEFAULT_MAX_AGE_S + 1000)
        if old is not None:
            failures.append("expired observations were still recalled")
        if again.prune(now=now + DEFAULT_MAX_AGE_S + 1000) != 3:
            failures.append("prune did not drop the expired observations")

        # A corrupt store must start empty, not raise.
        bad_store = Path(td) / "bad.json"
        bad_store.write_text("{not json", encoding="utf-8")
        try:
            broken = PlacementMemory(bad_store).load()
            if broken.stats()["observations"] != 0:
                failures.append("a corrupt store did not start empty")
        except Exception as exc:  # noqa: BLE001 - the point is that it must not raise
            failures.append(f"a corrupt store raised {exc!r}")

    for f in failures:
        print(f"  SELF-TEST FAIL: {f}")
    # ASCII on purpose: a tick mark raises UnicodeEncodeError on a cp1252
    # console AFTER every check has passed, which reports DEAD for a clean run.
    print("self-test: " + ("FAILED" if failures else "OK -- every rule still fails on its mutation"))
    return 1 if failures else 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(_self_test())
    mem = PlacementMemory().load()
    print(json.dumps(mem.stats(), indent=1))
