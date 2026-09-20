"""Tests for adk/builtin_tools.py — 21 built-in tool functions."""

import sys
import json
import os
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import adk.builtin_tools as bt

LF_ = chr(10)
B_LF = b"\n"
B_CRLF = b"\r\n"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_allowed_roots():
    """Reset the module-level allowed roots cache between tests."""
    bt._ALLOWED_ROOTS = None
    yield
    bt._ALLOWED_ROOTS = None


@pytest.fixture(autouse=True)
def reset_secrets_cache():
    """Reset secrets cache between tests."""
    bt._secrets_cache = None
    yield
    bt._secrets_cache = None


@pytest.fixture
def safe_dir(tmp_path, monkeypatch):
    """Set up a temporary directory that passes _is_safe_path."""
    monkeypatch.setattr(bt, "_DEFAULT_ALLOWED_ROOTS", [str(tmp_path)])
    return tmp_path


# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------

class TestPathSafety:
    def test_is_safe_path_within_allowed(self, safe_dir):
        p = safe_dir / "test.txt"
        assert bt._is_safe_path(str(p)) is True

    def test_is_safe_path_outside_allowed(self, safe_dir):
        # A path well outside the safe_dir
        assert bt._is_safe_path("/unlikely/random/path/outside") is False

    def test_get_allowed_roots_includes_env(self, safe_dir, monkeypatch):
        extra = str(safe_dir / "extra")
        monkeypatch.setenv("AITHER_ALLOWED_ROOTS", extra)
        roots = bt._get_allowed_roots()
        assert extra in roots

    def test_get_allowed_roots_empty_env(self, safe_dir, monkeypatch):
        monkeypatch.setenv("AITHER_ALLOWED_ROOTS", "")
        roots = bt._get_allowed_roots()
        assert str(safe_dir) in roots


# ---------------------------------------------------------------------------
# File I/O tools
# ---------------------------------------------------------------------------

class TestFileRead:
    def test_read_existing_file(self, safe_dir):
        f = safe_dir / "hello.txt"
        f.write_text("Hello World", encoding="utf-8")
        result = bt.file_read(str(f))
        assert result == "Hello World"

    def test_read_nonexistent_file(self, safe_dir):
        result = bt.file_read(str(safe_dir / "nope.txt"))
        data = json.loads(result)
        assert "error" in data
        assert "not found" in data["error"].lower()

    def test_read_outside_allowed_roots(self, safe_dir):
        result = bt.file_read("/unlikely/random/path/file.txt")
        data = json.loads(result)
        assert "error" in data
        assert "outside allowed roots" in data["error"].lower()

    def test_read_with_line_range(self, safe_dir):
        f = safe_dir / "lines.txt"
        f.write_text("line1\nline2\nline3\nline4\nline5", encoding="utf-8")
        result = bt.file_read(str(f), start_line=2, end_line=4)
        assert "line2" in result
        assert "line3" in result
        assert "line4" in result
        assert "line1" not in result

    def test_read_large_file_blocked(self, safe_dir):
        f = safe_dir / "big.bin"
        f.write_bytes(b"x" * (10_000_001))
        result = bt.file_read(str(f))
        data = json.loads(result)
        assert "error" in data
        assert "too large" in data["error"].lower()


class TestFileWrite:
    def test_write_new_file(self, safe_dir):
        target = safe_dir / "output.txt"
        result = bt.file_write(str(target), "content here")
        data = json.loads(result)
        assert data["success"] is True
        assert target.read_text(encoding="utf-8") == "content here"

    def test_write_append_mode(self, safe_dir):
        target = safe_dir / "append.txt"
        target.write_text("first", encoding="utf-8")
        bt.file_write(str(target), " second", mode="append")
        assert target.read_text(encoding="utf-8") == "first second"

    def test_write_creates_parent_dirs(self, safe_dir):
        target = safe_dir / "sub" / "dir" / "file.txt"
        result = bt.file_write(str(target), "nested")
        data = json.loads(result)
        assert data["success"] is True
        assert target.exists()

    def test_write_outside_allowed_roots(self, safe_dir):
        result = bt.file_write("/unlikely/random/path/file.txt", "bad")
        data = json.loads(result)
        assert "error" in data


class TestFileEdit:
    def test_edit_replaces_text(self, safe_dir):
        f = safe_dir / "edit.txt"
        f.write_text("Hello World", encoding="utf-8")
        result = bt.file_edit(str(f), "World", "Universe")
        data = json.loads(result)
        assert data["success"] is True
        assert f.read_text(encoding="utf-8") == "Hello Universe"

    def test_edit_nonexistent_file(self, safe_dir):
        result = bt.file_edit(str(safe_dir / "nope.txt"), "a", "b")
        data = json.loads(result)
        assert "error" in data
        assert "not found" in data["error"].lower()

    def test_edit_old_text_not_found(self, safe_dir):
        f = safe_dir / "edit2.txt"
        f.write_text("Hello", encoding="utf-8")
        result = bt.file_edit(str(f), "MISSING", "replacement")
        data = json.loads(result)
        assert "error" in data
        assert "not found" in data["error"].lower()

    def test_edit_ambiguous_multiple_matches(self, safe_dir):
        f = safe_dir / "dup.txt"
        f.write_text("aaa bbb aaa", encoding="utf-8")
        result = bt.file_edit(str(f), "aaa", "ccc")
        data = json.loads(result)
        assert "error" in data
        assert "2 times" in data["error"]

    def test_edit_outside_allowed_roots(self, safe_dir):
        result = bt.file_edit("/unlikely/random/path/file.txt", "a", "b")
        data = json.loads(result)
        assert "error" in data


class TestFileList:
    def test_list_directory(self, safe_dir):
        (safe_dir / "a.txt").touch()
        (safe_dir / "b.py").touch()
        (safe_dir / "subdir").mkdir()
        result = bt.file_list(str(safe_dir))
        data = json.loads(result)
        assert data["count"] >= 3
        names = [e["name"] for e in data["entries"]]
        assert "a.txt" in names
        assert "subdir" in names

    def test_list_with_pattern(self, safe_dir):
        (safe_dir / "a.txt").touch()
        (safe_dir / "b.py").touch()
        result = bt.file_list(str(safe_dir), pattern="*.py")
        data = json.loads(result)
        names = [e["name"] for e in data["entries"]]
        assert "b.py" in names
        assert "a.txt" not in names

    def test_list_nonexistent_dir(self, safe_dir):
        result = bt.file_list(str(safe_dir / "nonexistent"))
        data = json.loads(result)
        assert "error" in data

    def test_list_entries_have_type_and_size(self, safe_dir):
        f = safe_dir / "sized.txt"
        f.write_text("12345", encoding="utf-8")
        result = bt.file_list(str(safe_dir))
        data = json.loads(result)
        entry = next(e for e in data["entries"] if e["name"] == "sized.txt")
        assert entry["type"] == "file"
        assert entry["size"] == 5


class TestFileSearch:
    def test_search_by_name(self, safe_dir):
        (safe_dir / "alpha.py").write_text("hello", encoding="utf-8")
        (safe_dir / "beta.txt").write_text("world", encoding="utf-8")
        result = bt.file_search(str(safe_dir), "*.py")
        data = json.loads(result)
        assert data["count"] == 1
        assert "alpha.py" in data["results"][0]["path"]

    def test_search_with_content_pattern(self, safe_dir):
        (safe_dir / "a.txt").write_text("find me here", encoding="utf-8")
        (safe_dir / "b.txt").write_text("nothing to see", encoding="utf-8")
        result = bt.file_search(str(safe_dir), "*.txt", content_pattern="find me")
        data = json.loads(result)
        assert data["count"] == 1
        assert "a.txt" in data["results"][0]["path"]
        assert data["results"][0]["matches"][0]["line"] == 1

    def test_search_no_matches(self, safe_dir):
        (safe_dir / "a.txt").write_text("nothing", encoding="utf-8")
        result = bt.file_search(str(safe_dir), "*.py")
        data = json.loads(result)
        assert data["count"] == 0


# ---------------------------------------------------------------------------
# Shell execution
# ---------------------------------------------------------------------------

class TestShellExec:
    @patch("adk.builtin_tools.subprocess.run")
    def test_shell_exec_success(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="hello output",
            stderr="",
        )
        result = bt.shell_exec("echo hello")
        data = json.loads(result)
        assert data["exit_code"] == 0
        assert data["stdout"] == "hello output"

    @patch("adk.builtin_tools.subprocess.run")
    def test_shell_exec_nonzero_exit(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="error occurred",
        )
        result = bt.shell_exec("false")
        data = json.loads(result)
        assert data["exit_code"] == 1
        assert "error" in data["stderr"]

    @patch("adk.builtin_tools.subprocess.run")
    def test_shell_exec_timeout(self, mock_run):
        import subprocess
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="sleep 60", timeout=5)
        result = bt.shell_exec("sleep 60", timeout=5)
        data = json.loads(result)
        assert "error" in data
        assert "timed out" in data["error"].lower()

    @patch("adk.builtin_tools.subprocess.run")
    def test_shell_exec_truncates_long_output(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="x" * 60_000,
            stderr="e" * 20_000,
        )
        result = bt.shell_exec("big output")
        data = json.loads(result)
        assert len(data["stdout"]) <= 50_000
        assert len(data["stderr"]) <= 10_000


# ---------------------------------------------------------------------------
# Python execution
# ---------------------------------------------------------------------------

class TestPythonExec:
    def test_python_exec_captures_stdout(self):
        result = bt.python_exec("print('hello world')")
        data = json.loads(result)
        assert "hello world" in data["stdout"]

    def test_python_exec_captures_result_var(self):
        result = bt.python_exec("result = 42")
        data = json.loads(result)
        assert data["result"] == 42

    def test_python_exec_captures_errors(self):
        result = bt.python_exec("raise ValueError('boom')")
        data = json.loads(result)
        assert "ValueError" in data["stderr"]
        assert "boom" in data["stderr"]

    def test_python_exec_no_result_var(self):
        result = bt.python_exec("x = 10")
        data = json.loads(result)
        assert "result" not in data

    def test_python_exec_stderr_capture(self):
        result = bt.python_exec("import sys; sys.stderr.write('warning')")
        data = json.loads(result)
        assert "warning" in data["stderr"]


# ---------------------------------------------------------------------------
# Web tools (async)
# ---------------------------------------------------------------------------

class TestWebSearch:
    @pytest.mark.asyncio
    async def test_web_search_success(self):
        mock_html = (
            '<a class="result__a" href="https://example.com">Example Title</a>'
            '<span class="result__snippet">Example snippet text</a>'
        )
        mock_resp = MagicMock()
        mock_resp.text = mock_html
        mock_resp.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await bt.web_search("test query", limit=3)
            data = json.loads(result)
            assert data["query"] == "test query"

    @pytest.mark.asyncio
    async def test_web_search_tolerates_alias_and_stray_kwargs(self):
        # A small local model emits max_results= or an invented kwarg instead of
        # limit=. The tool must degrade to a real search, never raise TypeError
        # (which the agent loop surfaces as "no answer from the agent").
        mock_html = (
            '<a class="result__a" href="https://e.com/1">One</a>'
            '<span class="result__snippet">snip one</a>'
        )
        mock_resp = MagicMock()
        mock_resp.text = mock_html
        mock_resp.raise_for_status = MagicMock()
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            # alias, string limit, and an entirely unknown kwarg all succeed
            for kwargs in ({"max_results": 3}, {"limit": "4"}, {"count": 7, "n": 2}):
                result = await bt.web_search("today's news", **kwargs)
                assert json.loads(result)["query"] == "today's news"

    @pytest.mark.asyncio
    async def test_web_search_handles_error(self):
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=Exception("Connection failed"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await bt.web_search("test")
            data = json.loads(result)
            assert "error" in data


class TestWebFetch:
    @pytest.mark.asyncio
    async def test_web_fetch_success(self):
        mock_resp = MagicMock()
        mock_resp.text = "<html><body><p>Hello World</p></body></html>"
        mock_resp.raise_for_status = MagicMock()
        # The fetch ladder re-checks every redirect hop against the SSRF gate and
        # reads the final status, so a mocked response must look like a real one.
        mock_resp.url = "https://example.com"
        mock_resp.history = []
        mock_resp.status_code = 200

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await bt.web_fetch("https://example.com")
            assert "Hello World" in result

    @pytest.mark.asyncio
    async def test_web_fetch_truncates(self):
        mock_resp = MagicMock()
        mock_resp.text = "x" * 50_000
        mock_resp.raise_for_status = MagicMock()
        # The fetch ladder re-checks every redirect hop against the SSRF gate and
        # reads the final status, so a mocked response must look like a real one.
        mock_resp.url = "https://example.com"
        mock_resp.history = []
        mock_resp.status_code = 200

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await bt.web_fetch("https://example.com", max_chars=100)
            assert len(result) <= 100

    @pytest.mark.asyncio
    async def test_web_fetch_strips_html(self):
        mock_resp = MagicMock()
        mock_resp.text = "<script>alert('xss')</script><p>Clean text</p>"
        mock_resp.raise_for_status = MagicMock()
        # The fetch ladder re-checks every redirect hop against the SSRF gate and
        # reads the final status, so a mocked response must look like a real one.
        mock_resp.url = "https://example.com"
        mock_resp.history = []
        mock_resp.status_code = 200

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await bt.web_fetch("https://example.com")
            assert "alert" not in result
            assert "Clean text" in result

    @pytest.mark.asyncio
    async def test_web_fetch_handles_error(self):
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=Exception("timeout"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await bt.web_fetch("https://example.com")
            data = json.loads(result)
            assert "error" in data


# ---------------------------------------------------------------------------
# Secrets store
# ---------------------------------------------------------------------------

class TestSecrets:
    def test_secret_set_and_get(self, safe_dir, monkeypatch):
        secrets_file = safe_dir / "secrets.json"
        monkeypatch.setattr(bt, "_SECRETS_FILE", secrets_file)
        bt._secrets_cache = None

        set_result = bt.secret_set("MY_KEY", "my_value")
        data = json.loads(set_result)
        assert data["success"] is True

        bt._secrets_cache = None  # Force reload from disk
        val = bt.secret_get("MY_KEY")
        assert val == "my_value"

    def test_secret_get_from_env(self, monkeypatch):
        monkeypatch.setenv("MY_ENV_SECRET", "env_value")
        val = bt.secret_get("MY_ENV_SECRET")
        assert val == "env_value"

    def test_secret_get_not_found(self, safe_dir, monkeypatch):
        secrets_file = safe_dir / "secrets.json"
        monkeypatch.setattr(bt, "_SECRETS_FILE", secrets_file)
        bt._secrets_cache = None

        val = bt.secret_get("NONEXISTENT_KEY")
        data = json.loads(val)
        assert "error" in data
        assert "not found" in data["error"].lower()

    def test_secret_list(self, safe_dir, monkeypatch):
        secrets_file = safe_dir / "secrets.json"
        monkeypatch.setattr(bt, "_SECRETS_FILE", secrets_file)
        bt._secrets_cache = None

        bt.secret_set("KEY_A", "a")
        bt.secret_set("KEY_B", "b")

        result = bt.secret_list()
        data = json.loads(result)
        assert data["count"] == 2
        assert "KEY_A" in data["keys"]
        assert "KEY_B" in data["keys"]

    def test_secret_env_takes_priority(self, safe_dir, monkeypatch):
        secrets_file = safe_dir / "secrets.json"
        monkeypatch.setattr(bt, "_SECRETS_FILE", secrets_file)
        bt._secrets_cache = None

        bt.secret_set("DUALKEY", "file_value")
        monkeypatch.setenv("DUALKEY", "env_value")
        assert bt.secret_get("DUALKEY") == "env_value"


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

class TestRegistration:
    def test_tool_categories_defined(self):
        assert "file_io" in bt.TOOL_CATEGORIES
        assert "shell" in bt.TOOL_CATEGORIES
        assert "python" in bt.TOOL_CATEGORIES
        assert "web" in bt.TOOL_CATEGORIES
        assert "secrets" in bt.TOOL_CATEGORIES

    def test_file_io_has_five_tools(self):
        assert len(bt.TOOL_CATEGORIES["file_io"]) == 5

    def test_register_builtin_tools_explicit_categories(self):
        mock_agent = MagicMock()
        mock_agent.name = "test"
        mock_agent._tools = MagicMock()

        count = bt.register_builtin_tools(mock_agent, categories=["shell"])
        assert count == 1
        mock_agent._tools.register.assert_called_once_with(
            bt.shell_exec,
            intent_categories=bt.TOOL_INTENT_CATEGORIES.get(bt.shell_exec, []),
        )

    def test_register_builtin_tools_auto_for_demiurge(self):
        mock_agent = MagicMock()
        mock_agent.name = "demiurge"
        mock_agent._tools = MagicMock()

        count = bt.register_builtin_tools(mock_agent)
        expected_cats = bt.IDENTITY_DEFAULTS["demiurge"]
        # "self" category uses register_self_tools() (4 closure-based tools),
        # not the empty TOOL_CATEGORIES["self"] list.
        expected_count = sum(len(bt.TOOL_CATEGORIES[c]) for c in expected_cats)
        if "self" in expected_cats:
            expected_count += 4
        assert count == expected_count

    def test_register_builtin_tools_auto_unknown_identity(self):
        mock_agent = MagicMock()
        mock_agent.name = "unknown_agent"
        mock_agent._tools = MagicMock()

        count = bt.register_builtin_tools(mock_agent)
        # Defaults to ["file_io", "web", "decisions"] for unknown identities.
        # "decisions" IS in the minimal set on purpose — an agent that cannot reach
        # its owner has to guess, and a wrong guess is what that channel exists to
        # prevent. This test still named the older two-category default and had been
        # failing 11 == 7; derive the count from TOOL_CATEGORIES so adding a tool to
        # an existing category cannot break it again.
        expected = sum(
            len(bt.TOOL_CATEGORIES[c]) for c in ("file_io", "web", "decisions")
        )
        assert count == expected

    def test_register_builtin_tools_no_auto(self):
        mock_agent = MagicMock()
        mock_agent.name = "test"
        mock_agent._tools = MagicMock()

        count = bt.register_builtin_tools(mock_agent, auto=False)
        # Should register all categories; "self" uses register_self_tools (4 tools)
        total = sum(len(fns) for fns in bt.TOOL_CATEGORIES.values())
        if "self" in bt.TOOL_CATEGORIES:
            total += 4
        assert count == total

    def test_register_empty_category(self):
        mock_agent = MagicMock()
        mock_agent.name = "test"
        mock_agent._tools = MagicMock()

        count = bt.register_builtin_tools(mock_agent, categories=["nonexistent_cat"])
        assert count == 0

    def test_tool_functions_return_strings(self, safe_dir):
        """Every tool should return a string."""
        f = safe_dir / "test.txt"
        f.write_text("hello", encoding="utf-8")
        assert isinstance(bt.file_read(str(f)), str)
        assert isinstance(bt.file_write(str(f), "content"), str)
        assert isinstance(bt.file_edit(str(f), "content", "new"), str)
        assert isinstance(bt.file_list(str(safe_dir)), str)
        assert isinstance(bt.file_search(str(safe_dir), "*"), str)
        assert isinstance(bt.python_exec("x=1"), str)

    @patch("adk.builtin_tools.subprocess.run")
    def test_shell_exec_returns_string(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
        assert isinstance(bt.shell_exec("echo ok"), str)


# ---------------------------------------------------------------------------
# Repowise search
# ---------------------------------------------------------------------------

class TestRepowiseSearch:
    def test_repowise_categories_defined(self):
        assert "repowise" in bt.TOOL_CATEGORIES
        assert bt.repowise_search in bt.TOOL_CATEGORIES["repowise"]

    @patch("httpx.Client")
    def test_repowise_search_success(self, mock_client_cls):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "results": [
                {"file": "lib/core/agent.py", "symbol": "AgentKernel", "snippet": "class AgentKernel:", "score": 0.95},
                {"file": "lib/core/forge.py", "symbol": "AgentForge", "snippet": "class AgentForge:", "score": 0.87},
            ]
        }
        mock_client = MagicMock()
        mock_client.post.return_value = mock_resp
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client_cls.return_value = mock_client

        result = bt.repowise_search("agent kernel")
        data = json.loads(result)
        assert data["source"] == "repowise"
        assert data["count"] == 2
        assert data["results"][0]["file"] == "lib/core/agent.py"
        assert data["results"][0]["score"] == 0.95

    @patch("httpx.Client")
    def test_repowise_search_truncates_snippet(self, mock_client_cls):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "results": [{"file": "a.py", "symbol": "x", "snippet": "y" * 500, "score": 0.5}]
        }
        mock_client = MagicMock()
        mock_client.post.return_value = mock_resp
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client_cls.return_value = mock_client

        result = bt.repowise_search("query")
        data = json.loads(result)
        assert len(data["results"][0]["snippet"]) <= 200

    @patch("httpx.Client")
    def test_repowise_search_respects_max_results(self, mock_client_cls):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"results": []}
        mock_client = MagicMock()
        mock_client.post.return_value = mock_resp
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client_cls.return_value = mock_client

        bt.repowise_search("test", max_results=3)
        call_json = mock_client.post.call_args[1]["json"]
        assert call_json["limit"] == 3

    @patch("httpx.Client")
    @patch("adk.builtin_tools.code_search")
    def test_repowise_search_fallback_to_ripgrep(self, mock_code_search, mock_client_cls):
        """When Repowise is unavailable, falls back to code_search (ripgrep)."""
        mock_client = MagicMock()
        mock_client.post.side_effect = Exception("Connection refused")
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client_cls.return_value = mock_client
        mock_code_search.return_value = "file.py:10:match"

        result = bt.repowise_search("search term")
        mock_code_search.assert_called_once_with(pattern="search term", max_results=10)
        assert result == "file.py:10:match"

    @patch("httpx.Client")
    @patch("adk.builtin_tools.code_search")
    def test_repowise_search_fallback_on_non200(self, mock_code_search, mock_client_cls):
        """Non-200 status triggers ripgrep fallback."""
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_client = MagicMock()
        mock_client.post.return_value = mock_resp
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client_cls.return_value = mock_client
        mock_code_search.return_value = "(no matches)"

        result = bt.repowise_search("query")
        mock_code_search.assert_called_once()

    def test_repowise_search_custom_url(self, monkeypatch):
        """AITHER_REPOWISE_URL env var is respected."""
        monkeypatch.setenv("AITHER_REPOWISE_URL", "http://custom:9999")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"results": []}
        mock_client = MagicMock()
        mock_client.post.return_value = mock_resp
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)

        with patch("httpx.Client", return_value=mock_client):
            bt.repowise_search("test")
            url = mock_client.post.call_args[0][0]
            assert url.startswith("http://custom:9999")


# ---------------------------------------------------------------------------
# Swarm code
# ---------------------------------------------------------------------------

class TestSwarmCode:
    def test_swarm_categories_defined(self):
        assert "swarm" in bt.TOOL_CATEGORIES
        assert bt.swarm_code in bt.TOOL_CATEGORIES["swarm"]

    @patch("httpx.post")
    def test_swarm_code_success(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "status": "completed",
            "architect_plan": "Step 1: Design API\nStep 2: Implement",
            "code": "def handler(): pass",
            "tests": "def test_handler(): assert True",
            "artifacts": ["api.py"],
        }
        mock_post.return_value = mock_resp

        result = bt.swarm_code("Build a REST API endpoint")
        data = json.loads(result)
        assert data["status"] == "completed"
        assert "Design API" in data["plan"]
        assert "def handler" in data["code"]
        assert "api.py" in data["artifacts"]

    @patch("httpx.post")
    def test_swarm_code_sends_correct_params(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"status": "ok"}
        mock_post.return_value = mock_resp

        bt.swarm_code("task", mode="plan_only", effort=5)
        call_json = mock_post.call_args[1]["json"]
        assert call_json["problem"] == "task"
        assert call_json["mode"] == "plan_only"
        assert call_json["effort"] == 5

    @patch("httpx.post")
    def test_swarm_code_non200(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 503
        mock_post.return_value = mock_resp

        result = bt.swarm_code("task")
        data = json.loads(result)
        assert "error" in data
        assert "503" in data["error"]

    @patch("httpx.post")
    def test_swarm_code_connection_error(self, mock_post):
        mock_post.side_effect = Exception("Connection refused")

        result = bt.swarm_code("task")
        data = json.loads(result)
        assert "error" in data
        assert "Connection refused" in data["error"]

    @patch("httpx.post")
    def test_swarm_code_timeout(self, mock_post):
        import httpx
        mock_post.side_effect = httpx.TimeoutException("timed out")

        result = bt.swarm_code("task")
        data = json.loads(result)
        assert "error" in data

    def test_swarm_code_custom_genesis_url(self, monkeypatch):
        monkeypatch.setenv("AITHER_GENESIS_URL", "http://custom:9001")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"status": "ok"}

        with patch("httpx.post", return_value=mock_resp) as mock_post:
            bt.swarm_code("task")
            url = mock_post.call_args[0][0]
            assert url.startswith("http://custom:9001")


class TestFileToolsLineEndings:
    """The FILE decides its line ending, never the host (measured 2026-08-22:
    a Windows host turned one exact-match edit into a 616-line CRLF rewrite)."""

    def test_write_keeps_lf_on_any_host(self, safe_dir):
        target = safe_dir / "lf.py"
        bt.file_write(str(target), "a" + LF_ + "b" + LF_)
        assert target.read_bytes() == b"a" + B_LF + b"b" + B_LF

    def test_edit_preserves_crlf_file(self, safe_dir):
        target = safe_dir / "crlf.py"
        target.write_bytes(b"x = 1" + B_CRLF + b"y = 2" + B_CRLF + b"z = 3" + B_CRLF)
        res = json.loads(bt.file_edit(str(target), "y = 2", "y = 20"))
        assert res.get("success") is True, res
        raw = target.read_bytes()
        assert raw == b"x = 1" + B_CRLF + b"y = 20" + B_CRLF + b"z = 3" + B_CRLF
        assert raw.count(B_CRLF) == 3 and raw.count(B_LF) == 3  # no bare LF crept in

    def test_edit_preserves_lf_file(self, safe_dir):
        target = safe_dir / "lf2.py"
        target.write_bytes(b"x = 1" + B_LF + b"y = 2" + B_LF)
        bt.file_edit(str(target), "y = 2", "y = 20")
        raw = target.read_bytes()
        assert raw == b"x = 1" + B_LF + b"y = 20" + B_LF
        assert B_CRLF not in raw

    def test_edit_matches_multiline_old_text_against_crlf_file(self, safe_dir):
        target = safe_dir / "ml.py"
        target.write_bytes(b"def f():" + B_CRLF + b"    return 1" + B_CRLF)
        res = json.loads(bt.file_edit(str(target), "def f():" + LF_ + "    return 1",
                                      "def f():" + LF_ + "    return 2"))
        assert res.get("success") is True, res
        assert target.read_bytes() == b"def f():" + B_CRLF + b"    return 2" + B_CRLF

    def test_edit_refuses_noop(self, safe_dir):
        target = safe_dir / "noop.py"
        target.write_text("a = 1" + LF_)
        res = json.loads(bt.file_edit(str(target), "a = 1", "a = 1"))
        assert "error" in res and "identical" in res["error"]
        assert target.read_text() == "a = 1" + LF_


class TestShellExecDecoding:
    def test_shell_exec_survives_non_utf8_output(self, safe_dir):
        """Tool output is decoded utf-8 with replacement, never the locale codec.
        Measured 2026-08-23 on a SWE-bench-Live instance: a reader thread died with
        UnicodeDecodeError ('charmap' 0x81) on a Windows host, so the tool result
        was lost mid-run."""
        import sys
        cmd = sys.executable + ' -c "import sys; sys.stdout.buffer.write(b' + chr(39) + chr(92) + 'x81ok' + chr(39) + ')"'
        res = json.loads(bt.shell_exec(cmd))
        assert "error" not in res or res.get("exit_code") == 0, res
        assert "ok" in (res.get("stdout") or res.get("output") or json.dumps(res))


class TestQueueTools:
    """awrun-backed queue_* wrappers (TOOL_CATEGORIES['queue'], awdk[queue]).
    Isolated to a temp AITHER_AWRUN_DIR so this never touches a real queue."""

    @pytest.fixture(autouse=True)
    def isolated_queue_dir(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AITHER_AWRUN_DIR", str(tmp_path / "awrun"))
        yield

    def test_queue_status_malformed_id_returns_clean_error(self):
        """Regression for a real bug found live 2026-08-24 wiring the harness
        daemon's /awrun/status/{run_id} route: store.get() -> _locate() ->
        _validate_id() RAISES RunError on a malformed id instead of returning
        None, and queue_status did not catch it (unlike queue_bump/
        queue_cancel, which already do) — a malformed run_id crashed the
        whole call with an unhandled exception instead of the same clean
        {"error": ...} every other outcome of this function returns."""
        res = json.loads(bt.queue_status("not-a-real-id"))
        assert "error" in res
        assert "not-a-real-id" in res["error"]

    def test_queue_status_roundtrip(self):
        submitted = json.loads(bt.queue_submit(
            "ci", priority=8, workflow="deploy.yml", ref="develop",
        ))
        assert "id" in submitted
        status = json.loads(bt.queue_status(submitted["id"]))
        assert status["id"] == submitted["id"]
        assert status["priority"] == 8
        assert status["status"] == "queued"

    def test_queue_bump_and_cancel(self):
        submitted = json.loads(bt.queue_submit("ci", priority=1, workflow="x.yml"))
        bumped = json.loads(bt.queue_bump(submitted["id"], 10))
        assert bumped["priority"] == 10
        cancelled = json.loads(bt.queue_cancel(submitted["id"]))
        assert cancelled["status"] == "cancelled"
