"""Regressions a newcomer hit following awknowledge chapters 01 and 04 (2026-11-16)."""

from adk import cli, doctor


def test_tools_template_interpolates_query():
    # Written without .format(), so doubled braces printed a literal "{query}".
    assert "{{query}}" not in cli._TOOLS_TEMPLATE
    ns: dict = {}
    src = cli._TOOLS_TEMPLATE.replace("from adk import tool", "tool = lambda f: f")
    exec(src, ns)
    assert ns["search_docs"]("billing") == "Found docs matching: billing"


def test_agent_template_survives_non_utf8_console():
    # A cp1252 Windows console crashed print() on an emoji reply.
    rendered = cli._AGENT_TEMPLATE.format(name="lola")
    assert 'sys.stdout.reconfigure(encoding="utf-8"' in rendered
    compile(rendered, "agent.py", "exec")


def _r(name, passed):
    return {"name": name, "passed": passed, "severity": 0 if passed else 2,
            "summary": f"{name}: x", "output": "", "remedy": "r"}


def test_working_brain_makes_other_backends_alternatives():
    results = doctor.mark_brain_alternatives(
        [_r("Brain", True), _r("vLLM", False), _r("Docker", False), _r("Packs", False)])
    by = {r["name"]: r for r in results}
    assert by["vLLM"]["passed"] and by["Docker"]["passed"]
    assert "not needed" in by["vLLM"]["summary"]
    assert not by["Packs"]["passed"]  # unrelated failures still count


def test_no_brain_keeps_backend_failures():
    results = doctor.mark_brain_alternatives([_r("Brain", False), _r("vLLM", False)])
    assert not results[1]["passed"]


def test_backend_test_sends_message_objects():
    import inspect
    src = inspect.getsource(cli)
    assert '[{"role": "user", "content": "Say \'hello\' in one word."}]' not in src


def test_check_brain_works_inside_a_running_loop(monkeypatch):
    import asyncio

    class _P:
        base_url = "http://x"

        async def health_check(self):
            return True

    class _R:
        provider_name = "fake"

        def __init__(self, **_):
            pass

        async def get_provider(self):
            return _P()

    import adk.llm
    monkeypatch.setattr(adk.llm, "LLMRouter", _R)

    async def _inside():
        return doctor.check_brain()

    assert asyncio.run(_inside()) is True
