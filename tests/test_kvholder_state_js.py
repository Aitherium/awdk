"""The holder page's and swarm view's state rule (webui/kvholder/state.js), run under Node.

"Active" only while a call arrived in the last 5 s, and the rate shown is over that same window:
the page must never say Active next to 0 calls/s (the owner saw exactly that).
"""

from __future__ import annotations

import json
import random
import shutil
import subprocess

import pytest

from adk.kvholder_page import PAGE_HTML, STATE_JS, SWARM_HTML

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")


def _run(tmp_path, body: str):
    (tmp_path / "state.js").write_text(STATE_JS, encoding="utf-8")
    (tmp_path / "run.js").write_text(
        "const S = require('./state.js');\nconst out = [];\n" + body + "\n"
        "console.log(JSON.stringify(out));\n",
        encoding="utf-8",
    )
    r = subprocess.run(
        [NODE, "run.js"], cwd=tmp_path, capture_output=True, text=True, encoding="utf-8", timeout=60
    )
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_both_pages_use_the_shared_rule():
    for page in (PAGE_HTML, SWARM_HTML):
        assert 'src="state.js"' in page and "KVState.derive(" in page


def test_active_until_5_s_after_the_last_call_then_holding(tmp_path):
    out = _run(
        tmp_path,
        "const c = S.counter();\n"
        "let n = 0;\n"
        "for (let t = 0; t <= 10; t += 1) { n += 3; S.observe(c, t, n); }\n"  # last call at t=10
        "for (const t of [11, 12, 13, 14, 14.9, 15.1, 20, 75]) {\n"
        "  S.observe(c, t, n); out.push([t, S.derive('attached', true, 552, c, t)]);\n"
        "}\n",
    )
    by_t = {t: d for t, d in out}
    for t in (11, 12, 13, 14, 14.9):
        assert by_t[t]["state"] == "active" and by_t[t]["rate"] > 0, (t, by_t[t])
    for t in (15.1, 20, 75):
        assert by_t[t]["state"] == "holding" and by_t[t]["rate"] == 0, (t, by_t[t])
    assert abs(by_t[15.1]["ago"] - 5.1) < 1e-9 and abs(by_t[75]["ago"] - 65) < 1e-9


def test_ready_with_no_keys_and_waiting_before_the_engine(tmp_path):
    out = _run(
        tmp_path,
        "const c = S.counter(); S.observe(c, 0, 0); S.observe(c, 1, 0);\n"
        "out.push(S.derive('attached', true, 0, c, 1));\n"
        "out.push(S.derive('attached', false, 0, c, 1));\n"
        "out.push(S.derive('retrying', true, 99, c, 1));\n"
        "out.push(S.derive('attached', true, 0, S.counter(), 1));\n",
    )
    assert [d["state"] for d in out] == ["ready", "waiting", "retrying", "ready"]
    assert all(d["rate"] == 0 for d in out) and out[0]["ago"] is None


def test_never_active_at_zero_rate_and_active_iff_a_call_in_the_window(tmp_path):
    """Fuzz: irregular sampling (as a throttled phone tab polls), bursts and gaps."""
    rnd = random.Random(7)
    seqs = []
    for _ in range(40):
        t, n, pts = 0.0, 0, []
        for _ in range(120):
            t += rnd.choice([0.25, 0.5, 1.0, 1.0, 1.0, 2.0, 3.5, 7.0])
            if rnd.random() < 0.35:
                n += rnd.randint(1, 40)
            pts.append([round(t, 2), n])
        seqs.append(pts)
    out = _run(
        tmp_path,
        f"const seqs = {json.dumps(seqs)};\n"
        "for (const pts of seqs) { const c = S.counter(); const row = [];\n"
        "  for (const [t, n] of pts) { S.observe(c, t, n);\n"
        "    const d = S.derive('attached', true, 100, c, t); row.push([d.state, d.rate]); }\n"
        "  out.push(row); }\n",
    )
    for pts, row in zip(seqs, out):
        for i, (t, _) in enumerate(pts):
            state, rate = row[i]
            rose = [pts[j][0] for j in range(1, i + 1) if pts[j][1] > pts[j - 1][1]]
            want = bool(rose) and t - rose[-1] < 5
            assert (state == "active") is want, (t, state, rate)
            assert (rate > 0) is want, (t, state, rate)  # never Active beside 0 calls/s


def test_a_counter_that_goes_backwards_starts_over(tmp_path):
    out = _run(
        tmp_path,
        "const c = S.counter(); S.observe(c, 0, 50); S.observe(c, 1, 60);\n"
        "S.observe(c, 2, 0);\n"  # a new holder (or relay) behind the same key
        "out.push(S.derive('attached', true, 10, c, 2));\n",
    )
    assert out[0]["state"] == "holding" and out[0]["rate"] == 0
