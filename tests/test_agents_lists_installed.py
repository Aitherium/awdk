"""/agents lists every installed agent, not only the server's default identity."""
from adk.server import _installed_agents


def test_lists_installed_agent_dirs(tmp_path):
    for n in ("alpha", "beta", ".hidden", "_tmp"):
        (tmp_path / n).mkdir()
    (tmp_path / "alpha" / "agent.yaml").write_text("description: First agent\n", encoding="utf-8")
    (tmp_path / "stray.txt").write_text("x", encoding="utf-8")
    got = _installed_agents(tmp_path, exclude={"beta"})
    assert [a["name"] for a in got] == ["alpha"]
    assert got[0]["status"] == "installed"


def test_missing_root_is_empty_not_an_error(tmp_path):
    assert _installed_agents(tmp_path / "nope") == []


def test_unreadable_spec_still_listed(tmp_path):
    (tmp_path / "broken").mkdir()
    (tmp_path / "broken" / "agent.yaml").write_text(": : not yaml [", encoding="utf-8")
    got = _installed_agents(tmp_path)
    assert [a["name"] for a in got] == ["broken"]
