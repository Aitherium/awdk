"""The awsh daemon's /workforce reads the company brain on disk.

Measured 2026-10-02: the only path was Genesis over http://localhost:8001 (no host
port, no such route), so the OS said "workforce unreachable" while the roster sat
in .WORKFORCE/.DAO-COMPANY-BRAIN/brain/pack on the same machine.
"""

from adk.harnesses import agents


def _pack(tmp_path, specs):
    pack = tmp_path / "brain" / "pack"
    for name, body in specs.items():
        d = pack / name
        d.mkdir(parents=True)
        if body is not None:
            (d / "agent.yaml").write_text(body, encoding="utf-8")
    return tmp_path / "brain"


def test_reads_agents_from_the_pack(tmp_path):
    brain = _pack(tmp_path, {
        "atlas": "name: atlas\ndescription: >\n  Plans the\n  work\n",
        "voice-agent-template": "name: voice\n",
        "_draft": "name: draft\n",
        "nospec": None,
    })
    roster = agents.local_workforce([brain])
    assert [a["id"] for a in roster] == ["atlas"]
    assert roster[0]["role"] == "Plans the work"


def test_no_brain_anywhere_is_none(tmp_path):
    assert agents.local_workforce([tmp_path / "missing"]) is None


def test_fetch_prefers_the_local_brain_and_never_dials(tmp_path, monkeypatch):
    brain = _pack(tmp_path, {"hera": "name: hera\ndescription: growth\n"})
    monkeypatch.setenv("WORKFORCE_BRAIN_PATH", str(brain))

    def boom(*a, **k):
        raise AssertionError("must not dial Genesis when the brain is local")

    import httpx
    monkeypatch.setattr(httpx, "Client", boom)
    roster, reason = agents.fetch_workforce()
    assert reason == ""
    assert [a["id"] for a in roster] == ["hera"]


def test_an_explicit_base_url_still_goes_remote(tmp_path, monkeypatch):
    brain = _pack(tmp_path, {"hera": "name: hera\n"})
    monkeypatch.setenv("WORKFORCE_BRAIN_PATH", str(brain))
    roster, reason = agents.fetch_workforce("http://127.0.0.1:9")
    assert roster == [] and reason.startswith("workforce unreachable")
