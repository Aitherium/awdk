"""Windows Sandbox tool contracts -- mocked subprocess and network, no real `sbx`.

Every test here fails without the code it targets: locating, `--json`
placement, timeout reporting, hash verification, the detonate network gate,
host-path validation, the Windows-only guard, report parsing, and the tools
registered in the one DESKTOP_TOOLS table.
"""

import hashlib
import inspect
import io
import json
import subprocess
import zipfile
from pathlib import Path

import pytest

from adk.desktop import sandbox as sb


@pytest.fixture(autouse=True)
def on_windows(monkeypatch):
    monkeypatch.setattr(sb, "IS_WINDOWS", True)


def proc(stdout=b"{}", code=0, seen=None, raise_timeout=False):
    """A fake Runner. Records argv; optionally raises TimeoutExpired."""
    def run(argv, timeout):
        if seen is not None:
            seen.append(list(argv))
        if raise_timeout:
            raise subprocess.TimeoutExpired(argv, timeout)
        return subprocess.CompletedProcess(argv, code, stdout, b"")
    return run


@pytest.fixture
def sbx(tmp_path):
    """A release-layout install: sbx.cmd beside the bundled node.exe + src/cli.js."""
    path = tmp_path / "tools" / "1.2.0" / "sbx.cmd"
    path.parent.mkdir(parents=True)
    path.write_text("@echo off\r\n", encoding="utf-8")
    (path.parent / "node.exe").write_bytes(b"MZ")
    (path.parent / "src").mkdir()
    (path.parent / "src" / "cli.js").write_text("#!/usr/bin/env node\n", encoding="utf-8")
    return path


def release_zip(sbx_member="sandbox-mcp-win-x64-v1.2.0/sbx.cmd") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr(sbx_member, "@echo off\r\n")
        root = sbx_member.rsplit("/", 1)[0] + "/" if "/" in sbx_member else ""
        archive.writestr(root + "node.exe", b"MZ")
        archive.writestr(root + "src/cli.js", "#!/usr/bin/env node\n")
    return buf.getvalue()


def make_zip() -> bytes:
    return release_zip()


# -- locating sbx.cmd ----------------------------------------------------------

def test_locate_order_explicit_then_env_then_managed_then_path(tmp_path, monkeypatch, sbx):
    other = tmp_path / "elsewhere.cmd"
    other.write_text("", encoding="utf-8")
    monkeypatch.setenv(sb.ENV_OVERRIDE, str(other))
    assert sb.find_sbx(explicit=str(sbx), root=tmp_path) == sbx

    monkeypatch.delenv(sb.ENV_OVERRIDE)
    managed = tmp_path / "managed" / "1.2.0" / "sbx.cmd"
    managed.parent.mkdir(parents=True)
    managed.write_text("", encoding="utf-8")
    assert sb.find_sbx(root=tmp_path / "managed") == managed

    monkeypatch.setattr(sb.shutil, "which",
                        lambda name: str(other) if name == "sbx" else None)
    assert sb.find_sbx(root=tmp_path / "absent") == other


def test_managed_dirs_pick_the_highest_numeric_version(tmp_path):
    for version in ("1.2.0", "1.9.9", "1.10.0"):
        directory = tmp_path / version
        directory.mkdir()
        (directory / "sbx.cmd").write_text("", encoding="utf-8")
    found = sb.find_sbx(root=tmp_path)
    assert found is not None and found.parent.name == "1.10.0"


def test_missing_override_is_loud_not_a_silent_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv(sb.ENV_OVERRIDE, str(tmp_path / "gone.cmd"))
    with pytest.raises(sb.SandboxError) as caught:
        sb.find_sbx(root=tmp_path)
    assert caught.value.reason == "not_installed"


# -- argv, --json placement, timeout ------------------------------------------

def test_json_comes_before_positional_and_target_is_its_own_argv(sbx, tmp_path):
    seen = []
    target = tmp_path / "app.exe"
    target.write_bytes(b"MZ")
    result = sb.Sandbox(sbx=sbx, runner=proc(b'{"ok": true}', seen=seen)).deps(str(target))
    argv = seen[0]
    assert argv[argv.index("deps") + 1] == "--json"
    assert argv[-1] == str(target)
    assert argv.index("--json") < argv.index(str(target))
    assert result.ok and result.data == {"ok": True}


def test_bundled_node_runs_the_cli_with_no_cmd_exe(sbx):
    seen = []
    sb.Sandbox(sbx=sbx, runner=proc(seen=seen)).doctor()
    argv = seen[0]
    assert argv[0] == str(sbx.parent / "node.exe")
    assert argv[1] == str(sbx.parent / "src" / "cli.js")
    assert not any(str(part).lower().endswith("cmd.exe") for part in argv)
    assert argv[2] == "doctor"
    assert argv[argv.index("doctor") + 1] == "--json"


def test_a_shim_without_the_bundled_runtime_is_refused_not_sent_to_cmd(tmp_path):
    lone = tmp_path / "loose" / "sbx.cmd"
    lone.parent.mkdir(parents=True)
    lone.write_text("@echo off\r\n", encoding="utf-8")

    def must_not_run(argv, timeout):
        raise AssertionError("no process may start for an unverified layout")

    with pytest.raises(sb.SandboxError) as caught:
        sb.Sandbox(sbx=lone, runner=must_not_run).doctor()
    assert caught.value.reason == "not_installed"
    assert "cmd.exe" in str(caught.value)


def test_install_path_with_a_space_is_its_own_argv_element(tmp_path):
    base = tmp_path / "Program Files Jane" / "1.2.0"
    base.mkdir(parents=True)
    shim = base / "sbx.cmd"
    shim.write_text("@echo off\r\n", encoding="utf-8")
    (base / "node.exe").write_bytes(b"MZ")
    (base / "src").mkdir()
    (base / "src" / "cli.js").write_text("#!/usr/bin/env node\n", encoding="utf-8")
    seen = []
    sb.Sandbox(sbx=shim, runner=proc(seen=seen)).doctor()
    argv = seen[0]
    assert " " in argv[0] and argv[0] == str(base / "node.exe")
    assert argv[0] == str(base / "node.exe")
    assert argv[1] == str(base / "src" / "cli.js")
    assert not any(str(part).lower().endswith(("cmd.exe", "sbx.cmd")) for part in argv)


def test_timeout_is_reported_not_raised(sbx):
    result = sb.Sandbox(sbx=sbx, runner=proc(raise_timeout=True)).doctor()
    assert result.timed_out is True
    assert result.exit_code is None
    assert "timed out" in result.stderr


# -- detonate safety -----------------------------------------------------------

def test_detonate_defaults_to_network_off(sbx, tmp_path):
    seen = []
    target = tmp_path / "invoice.js"
    target.write_bytes(b"// suspicious")
    sb.Sandbox(sbx=sbx, runner=proc(seen=seen)).detonate(str(target))
    argv = seen[0]
    assert argv[argv.index("--network") + 1] == "off"
    assert argv[-1] == str(target)


def test_detonate_network_on_is_refused_without_allow_network(sbx, tmp_path):
    target = tmp_path / "invoice.js"
    target.write_bytes(b"// suspicious")

    def must_not_run(argv, timeout):
        raise AssertionError("detonate must be refused before it runs")

    with pytest.raises(sb.SandboxError) as caught:
        sb.Sandbox(sbx=sbx, runner=must_not_run).detonate(str(target), network="on")
    assert caught.value.reason == "refused"


def test_detonate_network_on_is_allowed_with_the_explicit_flag(sbx, tmp_path):
    seen = []
    target = tmp_path / "setup.exe"
    target.write_bytes(b"MZ")
    sb.Sandbox(sbx=sbx, runner=proc(seen=seen)).detonate(
        str(target), network="on", allow_network=True)
    assert seen[0][seen[0].index("--network") + 1] == "on"


def test_missing_target_is_refused(sbx, tmp_path):
    with pytest.raises(sb.SandboxError) as caught:
        sb.Sandbox(sbx=sbx, runner=proc()).detonate(str(tmp_path / "nope.exe"))
    assert caught.value.reason == "path_missing"


def test_detonate_refuses_a_folder(sbx, tmp_path):
    with pytest.raises(sb.SandboxError) as caught:
        sb.Sandbox(sbx=sbx, runner=proc()).detonate(str(tmp_path))
    assert caught.value.reason == "path_missing"


def test_run_accepts_a_folder_but_detonate_does_not(sbx, tmp_path):
    seen = []
    folder = tmp_path / "build"
    folder.mkdir()
    result = sb.Sandbox(sbx=sbx, runner=proc(seen=seen)).run_app(str(folder))
    assert seen and result.ok


def test_path_with_a_cmd_metacharacter_is_refused(tmp_path):
    evil = tmp_path / "evil & calc.exe"
    evil.write_bytes(b"MZ")
    with pytest.raises(sb.SandboxError) as caught:
        sb.check_host_path(str(evil))
    assert caught.value.reason == "bad_args"


# -- args travel as argv, never as cmd.exe syntax ------------------------------

#: The reviewer's cases: cmd.exe would split at ``&`` and expand ``%VAR%``.
HOSTILE_ARGS = ("&calc.exe", "%USERPROFILE%", "a & whoami | calc ^ !x!")


@pytest.mark.parametrize("hostile", HOSTILE_ARGS)
def test_detonate_args_reach_node_verbatim_as_one_element(sbx, tmp_path, hostile):
    seen = []
    target = tmp_path / "invoice.js"
    target.write_bytes(b"// suspicious")
    sb.Sandbox(sbx=sbx, runner=proc(seen=seen)).detonate(str(target), args=hostile)
    argv = seen[0]
    assert argv[0] == str(sbx.parent / "node.exe")
    assert not any(str(p).lower().endswith(("cmd.exe", "cmd")) for p in argv)
    assert argv[argv.index("--args") + 1] == hostile
    assert argv.count(hostile) == 1
    assert argv[-1] == str(target)


@pytest.mark.parametrize("hostile", HOSTILE_ARGS)
def test_run_args_reach_node_verbatim_as_one_element(sbx, tmp_path, hostile):
    seen = []
    folder = tmp_path / "build"
    folder.mkdir()
    sb.Sandbox(sbx=sbx, runner=proc(seen=seen)).run_app(str(folder), args=hostile)
    argv = seen[0]
    assert argv[0] == str(sbx.parent / "node.exe")
    assert not any(str(p).lower().endswith(("cmd.exe", "cmd")) for p in argv)
    assert argv[argv.index("--args") + 1] == hostile
    assert argv.count(hostile) == 1
    assert argv[-1] == str(folder)


@pytest.mark.parametrize("hostile", HOSTILE_ARGS)
def test_install_test_args_reach_node_verbatim_as_one_element(sbx, tmp_path, hostile):
    seen = []
    installer = tmp_path / "setup.msi"
    installer.write_bytes(b"MZ")
    sb.Sandbox(sbx=sbx, runner=proc(seen=seen)).install_test(
        str(installer), args=hostile)
    argv = seen[0]
    assert argv[0] == str(sbx.parent / "node.exe")
    assert not any(str(p).lower().endswith(("cmd.exe", "cmd")) for p in argv)
    assert argv[argv.index("--args") + 1] == hostile
    assert argv.count(hostile) == 1
    assert argv[-1] == str(installer)


def test_windows_only_guard(monkeypatch, sbx):
    monkeypatch.setattr(sb, "IS_WINDOWS", False)
    with pytest.raises(sb.SandboxError) as caught:
        sb.Sandbox(sbx=sbx, runner=proc()).doctor()
    assert caught.value.reason == "not_windows"


# -- install: verify before extract -------------------------------------------

def test_install_refuses_hash_mismatch_and_extracts_nothing(tmp_path, monkeypatch):
    payload = make_zip()

    def fake_get(url):
        if url.endswith(".sha256"):
            return ("0" * 64 + "  sandbox.zip\n").encode()
        if url.endswith(".zip"):
            return payload
        if "api.github.com" in url:
            return b'{"tag_name": "v1.2.0"}'
        raise AssertionError(url)

    monkeypatch.setattr(sb, "_extract_zip",
                        lambda *a, **k: pytest.fail("must not extract a bad hash"))
    with pytest.raises(sb.SandboxError) as caught:
        sb.install("latest", http_get=fake_get, root=tmp_path)
    assert caught.value.reason == "hash_mismatch"
    assert not (tmp_path / "1.2.0").exists()


def test_install_verifies_then_extracts_into_a_versioned_dir(tmp_path):
    payload = make_zip()
    digest = hashlib.sha256(payload).hexdigest()

    meta = json.dumps({"tag_name": "v1.2.0", "assets": [
        {"name": "sandbox-mcp-win-x64-v1.2.0.zip", "digest": "sha256:" + digest}]}).encode()

    def fake_get(url):
        if url.endswith(".sha256"):
            return ("%s  sandbox-mcp-win-x64-v1.2.0.zip\n" % digest).encode()
        if url.endswith(".zip"):
            return payload
        if "api.github.com" in url:
            return meta
        raise AssertionError(url)

    out = sb.install("latest", http_get=fake_get, root=tmp_path)
    assert out["ok"] is True and out["verified"] is True
    assert out["version"] == "1.2.0" and out["sha256"] == digest
    found = Path(out["sbx"])
    assert found.is_file() and found.name.lower() == "sbx.cmd"
    assert (tmp_path / "1.2.0") in found.parents


def test_install_refuses_when_the_api_digest_disagrees_with_the_sha256_asset(
        tmp_path, monkeypatch):
    payload = make_zip()
    real = hashlib.sha256(payload).hexdigest()
    meta = json.dumps({"tag_name": "v1.2.0", "assets": [
        {"name": "sandbox-mcp-win-x64-v1.2.0.zip", "digest": "sha256:" + "f" * 64}]}).encode()

    def fake_get(url):
        if url.endswith(".sha256"):
            return real.encode()
        if url.endswith(".zip"):
            return payload
        return meta

    monkeypatch.setattr(sb, "_extract_zip",
                        lambda *a, **k: pytest.fail("must not extract"))
    with pytest.raises(sb.SandboxError) as caught:
        sb.install("latest", http_get=fake_get, root=tmp_path)
    assert caught.value.reason == "hash_mismatch"
    assert "disagrees" in str(caught.value)
    assert not (tmp_path / "1.2.0").exists()


def test_install_refuses_a_version_that_escapes_the_install_root(tmp_path):
    def never_called(url):
        raise AssertionError("no download for a bad version")

    with pytest.raises(sb.SandboxError) as caught:
        sb.install("../../escape", http_get=never_called, root=tmp_path)
    assert caught.value.reason == "bad_args"


@pytest.mark.parametrize("member", [
    "../evil.cmd",              # relative climb
    "a/../../evil.cmd",         # climb from a subdirectory
    "/evil.cmd",                # absolute POSIX path
    "C:/evil.cmd",              # drive letter
    "C:\\evil.cmd",
])
def test_install_rejects_a_zip_member_that_escapes(tmp_path, monkeypatch, member):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr(member, "@echo off")
    payload = buf.getvalue()
    digest = hashlib.sha256(payload).hexdigest()

    def fake_get(url):
        if url.endswith(".sha256"):
            return digest.encode()
        if url.endswith(".zip"):
            return payload
        return b'{"tag_name": "v1.2.0"}'

    with pytest.raises(sb.SandboxError) as caught:
        sb.install("latest", http_get=fake_get, root=tmp_path)
    assert caught.value.reason == "extract_failed"
    assert not (tmp_path / "evil.cmd").exists()
    assert not (tmp_path.parent / "evil.cmd").exists()


def test_extract_refuses_too_many_members(tmp_path, monkeypatch):
    monkeypatch.setattr(sb, "MAX_ZIP_MEMBERS", 1)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("a.txt", "a")
        archive.writestr("b.txt", "b")
    with pytest.raises(sb.SandboxError) as caught:
        sb._extract_zip(buf.getvalue(), tmp_path / "out")
    assert caught.value.reason == "extract_failed"


def test_extract_refuses_an_oversized_archive(tmp_path, monkeypatch):
    monkeypatch.setattr(sb, "MAX_ZIP_BYTES", 10)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("big.bin", b"x" * 64)
    with pytest.raises(sb.SandboxError) as caught:
        sb._extract_zip(buf.getvalue(), tmp_path / "out")
    assert caught.value.reason == "extract_failed"


# -- download provenance -------------------------------------------------------

@pytest.mark.parametrize("url", [
    "http://github.com/shanselman/sandbox-mcp/releases/download/v1.2.0/x.zip",
    "https://evil.example/x.zip",
    "ftp://github.com/x.zip",
    "https://github.com.evil.example/x.zip",
    "file:///C:/x.zip",
])
def test_check_url_refuses_anything_but_https_on_the_github_hosts(url):
    with pytest.raises(sb.SandboxError) as caught:
        sb.check_url(url)
    assert caught.value.reason == "download_failed"


@pytest.mark.parametrize("url", [
    "https://github.com/shanselman/sandbox-mcp/releases/download/v1.2.0/x.zip",
    "https://api.github.com/repos/shanselman/sandbox-mcp/releases/latest",
    "https://objects.githubusercontent.com/abc?x=1",
    "https://release-assets.githubusercontent.com/abc",
])
def test_check_url_allows_the_release_hosts(url):
    assert sb.check_url(url) == url


def test_a_redirect_off_the_allowlist_is_refused():
    handler = sb._CheckedRedirect()
    with pytest.raises(sb.SandboxError) as caught:
        handler.redirect_request(None, None, 302, "Found", {},
                                 "https://evil.example/payload.zip")
    assert caught.value.reason == "download_failed"


def test_a_redirect_to_plain_http_is_refused():
    handler = sb._CheckedRedirect()
    with pytest.raises(sb.SandboxError) as caught:
        handler.redirect_request(None, None, 302, "Found", {},
                                 "http://github.com/x.zip")
    assert caught.value.reason == "download_failed"


def test_get_bytes_refuses_a_body_past_the_cap(monkeypatch):
    monkeypatch.setattr(sb, "MAX_DOWNLOAD_BYTES", 16)
    url = "https://github.com/x.zip"
    with pytest.raises(sb.SandboxError) as caught:
        sb._get_bytes(lambda u: b"x" * 17, url)
    assert caught.value.reason == "download_failed"


def test_read_capped_stops_at_the_limit():
    import io as _io

    class Body:
        def __init__(self, data):
            self._buf = _io.BytesIO(data)

        def read(self, n=-1):
            return self._buf.read(n)

    assert sb._read_capped(Body(b"abcd"), 4) == b"abcd"
    with pytest.raises(sb.SandboxError) as caught:
        sb._read_capped(Body(b"abcde"), 4)
    assert caught.value.reason == "download_failed"


# -- report parsing ------------------------------------------------------------

def test_read_report_parses_a_sample_report_json(tmp_path):
    run = tmp_path / "runs" / "20261110-120000"
    run.mkdir(parents=True)
    sample = {"summary": "3 red flag(s)", "verdict": None,
              "findings": [{"kind": "autorun", "path": "HKCU\\...\\Run\\X"}],
              "processes": [{"name": "powershell.exe", "pid": 8180}]}
    (run / "report.json").write_text(json.dumps(sample), encoding="utf-8")
    out = sb.read_report(run)
    assert out["found"] is True
    assert out["json"] == sample
    assert out["truncated"] is False
    assert "DATA" in out["data_note"]


def test_report_past_the_cap_is_a_preview_not_parsed(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    (run / "report.json").write_text(json.dumps({"blob": "x" * 1000}), encoding="utf-8")
    out = sb.read_report(run, max_chars=50)
    assert out["truncated"] is True and out["json"] is None
    assert len(out["text"]) == 50
    assert "untrusted evidence" in out["data_note"].lower()


def test_read_report_never_reads_the_whole_file(tmp_path, monkeypatch):
    """A guest-written multi-GB report must be read with a bound, not read_text()."""
    run = tmp_path / "run"
    run.mkdir()
    (run / "report.json").write_bytes(b'{"blob": "' + b"x" * 200_000 + b'"}')
    bounds = []
    real_open = Path.open

    class Spy:
        def __init__(self, handle):
            self._handle = handle

        def read(self, size=-1):
            assert size is not None and size >= 0, "read() must be given a bound"
            bounds.append(size)
            return self._handle.read(size)

        def __getattr__(self, name):
            return getattr(self._handle, name)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._handle.close()
            return False

    def spy_open(self, *args, **kwargs):
        return Spy(real_open(self, *args, **kwargs))

    monkeypatch.setattr(Path, "open", spy_open)
    out = sb.read_report(run, max_chars=100)
    assert out["truncated"] is True
    assert len(out["text"]) == 100
    assert bounds == [101]


def test_run_reads_the_report_from_the_named_run_dir(sbx, tmp_path):
    run = tmp_path / "runs" / "t1"
    run.mkdir(parents=True)
    (run / "report.json").write_text('{"summary": "ok"}', encoding="utf-8")
    payload = json.dumps({"runDir": str(run), "exitCode": 0}).encode()
    result = sb.Sandbox(sbx=sbx, runner=proc(payload)).deps(str(tmp_path))
    assert result.run_dir == str(run)
    assert result.report["json"] == {"summary": "ok"}


def test_parse_json_output_tolerates_a_trailing_log_line():
    assert sb.parse_json_output('log line\n{"a": 1}\n') == {"a": 1}
    with pytest.raises(ValueError):
        sb.parse_json_output("not json at all")


# -- the tool table and tool-layer receipts -----------------------------------

def test_all_six_sandbox_tools_are_in_the_one_table():
    from adk.desktop import tools as dt

    names = {fn.__name__ for fn, _class in dt.DESKTOP_TOOLS}
    assert {n for n in names if n.startswith("desktop_sandbox_")} == {
        "desktop_sandbox_doctor", "desktop_sandbox_install", "desktop_sandbox_detonate",
        "desktop_sandbox_run", "desktop_sandbox_install_test", "desktop_sandbox_deps"}
    table = dt.desktop_tool_table()
    # Anything that boots a VM, runs untrusted code or writes host artifacts is
    # write-class; only the two read-only queries are not.
    for name in ("desktop_sandbox_install", "desktop_sandbox_detonate",
                 "desktop_sandbox_run", "desktop_sandbox_install_test"):
        assert table[name][1] == "write", name
    for name in ("desktop_sandbox_doctor", "desktop_sandbox_deps"):
        assert table[name][1] == "", name
    assert inspect.signature(dt.desktop_sandbox_install).parameters["dry_run"].default is True
    detonate = inspect.signature(dt.desktop_sandbox_detonate).parameters
    assert detonate["network"].default == "off"
    assert detonate["allow_network"].default is False


def test_sandbox_execution_tool_refuses_an_unconfirmed_call(tmp_path, monkeypatch):
    """The route gate: detonate/run/install_test cannot run without confirm=true."""
    from adk.desktop import tools as dt
    from adk.harnesses import desktop_routes as dr

    target = tmp_path / "x.exe"
    target.write_bytes(b"MZ")

    class MustNotRun:
        def __init__(self, *a, **k):
            raise AssertionError("no sandbox may start without confirm=true")

    monkeypatch.setattr(sb, "Sandbox", MustNotRun)
    table = {fn.__name__: (fn, cls) for fn, cls in dt.DESKTOP_TOOLS}
    for name, args in (("desktop_sandbox_detonate", {"target": str(target)}),
                       ("desktop_sandbox_run", {"target": str(target)}),
                       ("desktop_sandbox_install_test", {"installer": str(target)})):
        status, out = dr.invoke(name, {"args": args}, table)
        assert status == 403 and out["reason"] == "confirm_required", name


def test_tool_maps_not_windows_to_named_json(monkeypatch):
    from adk.desktop import tools as dt

    monkeypatch.setattr(sb, "IS_WINDOWS", False)
    out = json.loads(dt.desktop_sandbox_doctor())
    assert out["ok"] is False and out["reason"] == "not_windows"


def test_tool_refuses_detonate_network_on(tmp_path):
    from adk.desktop import tools as dt

    target = tmp_path / "x.exe"
    target.write_bytes(b"MZ")
    out = json.loads(dt.desktop_sandbox_detonate(str(target), network="on"))
    assert out["ok"] is False and out["reason"] == "refused"


def test_tool_receipt_labels_findings_as_heuristics(monkeypatch, tmp_path):
    from adk.desktop import tools as dt

    class Fake:
        def detonate(self, *args, **kwargs):
            return sb.SbXResult("detonate", [], 0, data={"findings": []})

    monkeypatch.setattr(sb, "Sandbox", lambda *a, **k: Fake())
    out = json.loads(dt.desktop_sandbox_detonate(str(tmp_path / "x.exe")))
    assert out["ok"] is True
    assert out["findings_are"] == "heuristics, not a verdict"


# -- a capped receipt still carries the envelope a client needs ---------------
#
# The fields below are the ones sandbox-mcp v1.2.0 src/detonate.js writes: tree,
# lookups, connections, diff.{files,values,services} and findings.

_TASKS = "C:\\Windows\\System32\\Tasks\\"


def _big_detonate_result(sha="a" * 64):
    """A detonate result whose JSON (data repeated in report) is past the cap."""
    data = {
        "file": "sample.exe", "sha256": sha, "exitCode": 0,
        "runDir": "C:\\runs\\t1", "command": "sample.exe",
        "tree": [{"pid": i, "path": "C:\\x.exe", "commandLine": "x" * 4000}
                 for i in range(3)],
        "lookups": [{"name": "example.com"}] * 2,
        "connections": [{"address": "1.2.3.4", "port": 80}] * 4,
        "diff": {
            "files": {"added": [{"path": _TASKS + "One"}, {"path": _TASKS + "Two"},
                                {"path": "C:\\Users\\x\\a.txt"}]},
            "values": [{"name": "HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run\\X"}],
            "services": [{"change": "added", "name": "svc"},
                         {"change": "removed", "name": "old"}],
        },
        "findings": [{"level": "high", "text": "y" * 3000}],
    }
    return sb.SbXResult(command="sbx detonate --json sample.exe", argv=[], exit_code=0,
                        data=data, run_dir="C:\\runs\\t1",
                        report={"found": True, "truncated": False, "json": data})


def test_capped_receipt_keeps_its_envelope_and_counts():
    from adk.desktop import tools as dt

    out = json.loads(dt._sandbox_receipt(_big_detonate_result()))
    assert out["truncated"] is True
    assert out["ok"] is True
    assert out["command"] == "sbx detonate --json sample.exe"
    assert out["exit_code"] == 0 and out["timed_out"] is False
    assert out["run_dir"] == "C:\\runs\\t1"
    assert out["sha256"] == "a" * 64
    assert out["findings_are"] == "heuristics, not a verdict"
    assert out["error"] == ""
    assert out["counts"] == {"processes": 3, "lookups": 2, "connections": 4,
                             "services_added": 1, "persistence_values": 1,
                             "scheduled_tasks": 2, "findings": 1}
    # The bulky parts and the guest report are the only things dropped.
    assert "data" not in out and "report" not in out
    assert out["preview"].startswith('{"ok":')
    assert len(json.dumps(out)) <= dt._MAX_SANDBOX_CHARS


def test_capped_summary_is_present_even_when_no_counts_were_reported():
    from adk.desktop import tools as dt

    r = sb.SbXResult(command="sbx deps --json", argv=[], exit_code=1, timed_out=True,
                     data={"items": ["z" * 30000]})
    out = json.loads(dt._sandbox_receipt(r))
    assert out["ok"] is False and out["timed_out"] is True
    assert out["exit_code"] == 1 and out["error"] == "exit 1"
    assert out["run_dir"] is None and out["sha256"] is None
    assert out["counts"] == {"processes": None, "lookups": None, "connections": None,
                             "services_added": None, "persistence_values": None,
                             "scheduled_tasks": None, "findings": None}
    assert len(json.dumps(out)) <= dt._MAX_SANDBOX_CHARS


def test_capped_digest_comes_from_the_cli_never_from_the_guest_report():
    from adk.desktop import tools as dt

    r = _big_detonate_result()
    r.data.pop("sha256")
    # report.json is a separate guest-written copy that claims a different digest.
    r.report = {"found": True, "truncated": False,
                "json": {**r.data, "sha256": "b" * 64}}
    out = json.loads(dt._sandbox_receipt(r))
    assert out["truncated"] is True and out["sha256"] is None


def test_uncapped_receipt_shape_is_unchanged():
    from adk.desktop import tools as dt

    out = json.loads(dt._sandbox_receipt(
        sb.SbXResult("doctor", [], 0, data={"ready": True})))
    assert set(out) == {"ok", "command", "exit_code", "timed_out", "data",
                        "run_dir", "report", "findings_are", "error"}


def test_install_tool_dry_run_downloads_no_zip(monkeypatch):
    from adk.desktop import tools as dt

    calls = []

    def fake_get(url):
        calls.append(url)
        return b'{"tag_name": "v1.2.0"}'

    monkeypatch.setattr(sb, "default_http_get", fake_get)
    out = json.loads(dt.desktop_sandbox_install(version="latest", dry_run=True))
    assert out["ok"] is True and out["dry_run"] is True
    assert out["version"] == "1.2.0"
    assert calls and all(not url.endswith(".zip") for url in calls)


# -- network and device paths are refused before any filesystem call -----------

@pytest.mark.parametrize("raw", [
    r"\\attacker\share\x.exe",
    "//attacker/share/x.exe",
    r"\\?\C:\Windows\notepad.exe",
    r"\\.\PhysicalDrive0",
])
def test_network_and_device_paths_are_refused_without_a_stat(monkeypatch, raw):
    def no_stat(*_a, **_k):
        raise AssertionError("the filesystem was touched for %r" % raw)
    monkeypatch.setattr(sb.Path, "exists", no_stat)
    monkeypatch.setattr(sb.Path, "is_file", no_stat)
    monkeypatch.setattr(sb.Path, "is_dir", no_stat)
    with pytest.raises(sb.SandboxError) as caught:
        sb.check_host_path(raw)
    assert caught.value.reason == "bad_args"


def test_a_metacharacter_path_is_refused_before_a_stat(monkeypatch):
    def no_stat(*_a, **_k):
        raise AssertionError("the filesystem was touched")
    monkeypatch.setattr(sb.Path, "exists", no_stat)
    with pytest.raises(sb.SandboxError) as caught:
        sb.check_host_path(r"C:\tmp\a&b.exe")
    assert caught.value.reason == "bad_args"


def test_capped_persistence_counts_autorun_values_only():
    """An environment-variable change or a removed value is not persistence."""
    from adk.desktop import tools as dt

    r = _big_detonate_result()
    bs = chr(92)
    r.data["diff"]["values"] = [
        {"name": bs.join(["HKCU", "Software", "Microsoft", "Windows", "CurrentVersion", "Run", "X"])},
        {"name": bs.join(["HKLM", "SOFTWARE", "Microsoft", "Windows NT", "CurrentVersion", "Winlogon", "Shell"])},
        {"name": bs.join(["HKCU", "Environment", "PATH"])},
        {"name": bs.join(["HKCU", "Software", "Microsoft", "Windows", "CurrentVersion", "RunOnce", "Y"]),
         "change": "removed"},
    ]
    out = json.loads(dt._sandbox_receipt(r))
    assert out["truncated"] is True
    assert out["counts"]["persistence_values"] == 2


def test_capped_receipt_stays_under_the_cap_with_a_huge_error():
    from adk.desktop import tools as dt

    r = sb.SbXResult(command="sbx detonate --json sample.exe", argv=[], exit_code=2,
                     data={"tree": [{"commandLine": "x" * 30000}]}, stderr="E" * 50000)
    out = json.loads(dt._sandbox_receipt(r))
    assert out["truncated"] is True
    assert out["error"].startswith("EEE")
    assert len(out["error"]) <= dt._MAX_SANDBOX_ERROR_CHARS
    assert len(json.dumps(out)) <= dt._MAX_SANDBOX_CHARS
