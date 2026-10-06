"""adk awconnect -- status from real-shaped browser profiles, source choice, and the
checksum refusal.

What must hold and is not obvious from the source:
  - status reads BOTH Preferences and Secure Preferences (Chrome moved unpacked
    extensions into the latter) and names installed / disabled / absent / stale;
  - a release asset whose bytes do not hash to its .sha256 is REFUSED, and a
    release with no .sha256 at all is never a candidate;
  - a strictly newer checkout beats an older release (no silent downgrade);
  - install opens the browser with the extensions URL, puts the folder on the
    clipboard, and reports the two clicks -- all without a real browser.
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from pathlib import Path

import pytest
from adk import awconnect_setup as aw


def _env(tmp_path: Path) -> dict:
    return {"AITHER_AWCONNECT_HOME": str(tmp_path / "awc"), "AITHER_HOME": str(tmp_path / "home")}


def _staged(tmp_path: Path, version: str = "3.8.0") -> Path:
    cur = tmp_path / "awc" / "current"
    cur.mkdir(parents=True)
    (cur / "manifest.json").write_text(
        json.dumps({"manifest_version": 3, "name": "awconnect — AI Chat", "version": version})
    )
    (cur / aw.MARKER_FILE).write_text(json.dumps({"version": version, "source": "checkout"}))
    return cur


def _browser(tmp_path: Path, prefs: dict, secure: dict | None = None, profile: str = "Default"):
    ud = tmp_path / "chrome-ud"
    prof = ud / profile
    prof.mkdir(parents=True, exist_ok=True)
    (prof / "Preferences").write_text(json.dumps(prefs))
    if secure is not None:
        (prof / "Secure Preferences").write_text(json.dumps(secure))
    return aw.Browser("chrome", "Google Chrome", "chrome.exe", ud)


def _ext(entry: dict) -> dict:
    return {"extensions": {"settings": {"abcdefghijklmnopabcdefghijklmnop": entry}}}


# ── status ───────────────────────────────────────────────────────────────────


def test_status_installed_from_secure_preferences(tmp_path):
    cur = _staged(tmp_path)
    br = _browser(
        tmp_path,
        {"extensions": {"settings": {}}},
        _ext({"location": 4, "path": str(cur), "disable_reasons": []}),
    )
    st = aw.status(_env(tmp_path), [br])
    assert st["state"] == "installed"
    assert st["installed"] is True
    hit = st["hits"][0]
    assert hit["version"] == "3.8.0" and hit["uses_current"] and hit["unpacked"]
    assert hit["profile"] == "Default"
    assert "installed" in aw.status_line(st)


def test_status_disabled(tmp_path):
    cur = _staged(tmp_path)
    br = _browser(tmp_path, _ext({"location": 4, "path": str(cur), "disable_reasons": [1]}))
    st = aw.status(_env(tmp_path), [br])
    assert st["state"] == "disabled"
    assert st["installed"] is False
    assert "disabled" in aw.status_line(st)


def test_status_disabled_legacy_state_zero(tmp_path):
    cur = _staged(tmp_path)
    br = _browser(tmp_path, _ext({"location": 4, "path": str(cur), "state": 0}))
    assert aw.status(_env(tmp_path), [br])["state"] == "disabled"


def test_status_absent(tmp_path):
    _staged(tmp_path)
    other = tmp_path / "other-ext"
    other.mkdir()
    (other / "manifest.json").write_text(
        json.dumps({"manifest_version": 3, "name": "uBlock", "version": "1"})
    )
    br = _browser(tmp_path, _ext({"location": 4, "path": str(other)}))
    st = aw.status(_env(tmp_path), [br])
    assert st["state"] == "not_installed" and st["hits"] == []
    assert "not installed" in aw.status_line(st)


def test_status_stale_elsewhere_by_manifest_name(tmp_path):
    _staged(tmp_path, "3.9.0")
    old = tmp_path / "old-tree"
    old.mkdir()
    (old / "manifest.json").write_text(
        json.dumps({"manifest_version": 3, "name": "awconnect — AI Chat", "version": "3.8.0"})
    )
    br = _browser(tmp_path, _ext({"location": 4, "path": str(old)}), profile="Profile 2")
    st = aw.status(_env(tmp_path), [br])
    assert st["state"] == "stale"
    hit = st["hits"][0]
    assert hit["stale"] and not hit["uses_current"] and hit["profile"] == "Profile 2"
    assert "3.9.0" in aw.status_line(st)


def test_status_survives_corrupt_preferences(tmp_path):
    _staged(tmp_path)
    ud = tmp_path / "ud"
    (ud / "Default").mkdir(parents=True)
    (ud / "Default" / "Preferences").write_text("{not json")
    st = aw.status(_env(tmp_path), [aw.Browser("edge", "Edge", None, ud)])
    assert st["state"] == "not_installed"


# ── sources ──────────────────────────────────────────────────────────────────


def _zip_bytes(version: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "manifest.json",
            json.dumps({"manifest_version": 3, "name": "awconnect", "version": version}),
        )
        zf.writestr("background.js", "// bg\n")
    return buf.getvalue()


def _fake_release(version: str, blob: bytes, sha: str | None):
    name = f"aither-connect-enterprise-v{version}.zip"
    assets = [{"name": name, "browser_download_url": f"https://example.test/{name}"}]
    if sha is not None:
        assets.append(
            {
                "name": f"{name}.sha256",
                "browser_download_url": f"https://example.test/{name}.sha256",
            }
        )
    api = json.dumps(
        [
            {"tag_name": "shell-v9.9.9", "assets": []},
            {"tag_name": f"connect-v{version}", "assets": assets},
            {"tag_name": "connect-v1.0.0", "draft": True, "assets": assets},
        ]
    ).encode()
    files = {aw.DEFAULT_RELEASES_API: api, f"https://example.test/{name}": blob}
    if sha is not None:
        files[f"https://example.test/{name}.sha256"] = f"{sha}  {name}\n".encode()

    def fetch(url: str) -> bytes:
        if url not in files:
            raise OSError(f"404 {url}")
        return files[url]

    return fetch


def test_release_selected_and_verified(tmp_path):
    blob = _zip_bytes("3.9.0")
    fetch = _fake_release("3.9.0", blob, hashlib.sha256(blob).hexdigest())
    rel = aw.latest_release(fetch)
    assert rel is not None and rel.version == "3.9.0" and rel.kind == "release"
    staged = aw.stage(rel, _env(tmp_path), fetch=fetch)
    assert staged["version"] == "3.9.0" and staged["source"] == "release"
    assert (tmp_path / "awc" / "current" / "background.js").is_file()
    assert (tmp_path / "awc" / "3.9.0" / "manifest.json").is_file()


def test_release_checksum_mismatch_is_refused(tmp_path):
    blob = _zip_bytes("3.9.0")
    fetch = _fake_release("3.9.0", blob, "0" * 64)
    rel = aw.latest_release(fetch)
    with pytest.raises(aw.ChecksumMismatchError):
        aw.stage(rel, _env(tmp_path), fetch=fetch)
    assert not (tmp_path / "awc" / "current").exists()


def test_release_without_checksum_is_not_a_candidate(tmp_path):
    blob = _zip_bytes("3.9.0")
    assert aw.latest_release(_fake_release("3.9.0", blob, None)) is None


def test_variant_preference_prefers_unpacked_over_public():
    """Measured 2026-10-06: staging the PUBLIC (keyless) zip gave the browser a
    path-derived extension id and sign-in answered "Invalid redirect_uri"; the
    unpacked build carries the manifest key and keeps the pinned id. The
    default chain must therefore be variant -> unpacked -> public."""
    version = "9.9.9"
    blob = _zip_bytes(version)
    sha = hashlib.sha256(blob).hexdigest()
    names = [f"aither-connect-{v}-v{version}.zip" for v in ("unpacked", "public")]
    assets = []
    for n in names:
        assets.append({"name": n, "browser_download_url": f"https://example.test/{n}"})
        assets.append(
            {"name": f"{n}.sha256", "browser_download_url": f"https://example.test/{n}.sha256"}
        )
    api = json.dumps([{"tag_name": f"connect-v{version}", "assets": assets}]).encode()
    files = {aw.DEFAULT_RELEASES_API: api}
    for n in names:
        files[f"https://example.test/{n}"] = blob
        files[f"https://example.test/{n}.sha256"] = f"{sha}  {n}\n".encode()

    def fetch(url: str) -> bytes:
        return files[url]

    rel = aw.latest_release(fetch)
    assert rel is not None and rel.location.endswith("-unpacked-v9.9.9.zip")
    # An explicit --variant public still stages exactly the public zip.
    explicit = aw.latest_release(fetch, variant="public")
    assert explicit is not None and explicit.location.endswith("-public-v9.9.9.zip")


def test_unreadable_release_api_is_no_source():
    def fetch(url):
        raise OSError("HTTP 404 (private repo)")

    assert aw.latest_release(fetch) is None


def test_zip_slip_refused(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../evil.txt", "x")
    blob = buf.getvalue()
    cand = aw.Candidate("release", "1.0.0", "https://x/a.zip", sha256_url="https://x/a.zip.sha256")
    files = {
        "https://x/a.zip": blob,
        "https://x/a.zip.sha256": hashlib.sha256(blob).hexdigest().encode(),
    }
    with pytest.raises(aw.AwconnectError):
        aw.stage(cand, _env(tmp_path), fetch=files.__getitem__)
    assert not (tmp_path / "awc" / "evil.txt").exists()


def test_choose_source_prefers_release_unless_checkout_newer():
    rel = aw.Candidate("release", "3.8.0", "u")
    new = aw.Candidate("checkout", "3.9.0", "r")
    same = aw.Candidate("checkout", "3.8.0", "r")
    assert aw.choose_source(rel, new).kind == "checkout"
    assert aw.choose_source(rel, same).kind == "release"
    assert aw.choose_source(None, same).kind == "checkout"
    assert aw.choose_source(rel, None).kind == "release"
    assert aw.choose_source(None, None) is None


def test_checkout_copy_fallback_drops_tests_and_keys(tmp_path):
    repo = tmp_path / "repo"
    src = repo / "awconnect"
    (src / "tests").mkdir(parents=True)
    (src / "manifest.json").write_text(
        json.dumps({"manifest_version": 3, "name": "awconnect", "version": "3.8.1"})
    )
    (src / "tests" / "x.test.mjs").write_text("t")
    (src / "key.pem").write_text("k")
    (src / "background.js").write_text("b")

    def no_git(*a, **k):
        raise OSError("git missing")

    cand = aw.checkout_candidate(repo)
    staged = aw.stage(cand, _env(tmp_path), run=no_git)
    cur = Path(staged["path"])
    assert (cur / "background.js").is_file()
    assert not (cur / "tests").exists() and not (cur / "key.pem").exists()
    assert staged["method"] == "copy"


def _dir_source(tmp_path: Path, version: str) -> "aw.Candidate":
    src = tmp_path / "src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "manifest.json").write_text(
        json.dumps({"manifest_version": 3, "name": "awconnect", "version": version})
    )
    (src / "background.js").write_text("b")
    return aw.Candidate("dir", version, str(src))


@pytest.mark.parametrize(
    "bad", ["../victim", "current", "1.2.3.4.5", "", "1.2/../../victim", "abs"]
)
def test_stage_refuses_unsafe_manifest_version(tmp_path, bad):
    # Review P1: stage() joined the manifest version onto the install root and
    # rmtree'd it, so "../victim" (or an absolute path) deleted a foreign dir.
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "important.txt").write_text("keep me")
    if bad == "abs":
        bad = str(victim)
    with pytest.raises(aw.AwconnectError):
        aw.stage(_dir_source(tmp_path, bad), _env(tmp_path))
    assert (victim / "important.txt").read_text() == "keep me"
    assert not (tmp_path / "awc" / "current" / "manifest.json").exists()


def test_stage_accepts_chrome_version(tmp_path):
    staged = aw.stage(_dir_source(tmp_path, "3.8.12.1"), _env(tmp_path))
    assert Path(staged["versioned_path"]) == tmp_path / "awc" / "3.8.12.1"
    assert (tmp_path / "awc" / "current" / "background.js").is_file()


def test_safe_version_dir_rejects_traversal(tmp_path):
    for bad in ("../x", "/etc", r"C:\Users\x", "current", "1..2"):
        with pytest.raises(aw.AwconnectError):
            aw.safe_version_dir(tmp_path, bad)
    assert aw.safe_version_dir(tmp_path, "1.2") == tmp_path / "1.2"


# ── install flow ─────────────────────────────────────────────────────────────


def test_install_opens_browser_copies_path_and_waits(tmp_path):
    repo = tmp_path / "repo"
    (repo / "awconnect").mkdir(parents=True)
    (repo / "awconnect" / "manifest.json").write_text(
        json.dumps({"manifest_version": 3, "name": "awconnect", "version": "3.8.0"})
    )
    env = {**_env(tmp_path), "AITHEROS_ROOT": str(repo)}
    ud = tmp_path / "ud"
    (ud / "Default").mkdir(parents=True)
    (ud / "Default" / "Preferences").write_text("{}")
    br = aw.Browser("chrome", "Google Chrome", "C:/chrome.exe", ud)

    launched, clip = [], []

    def run(cmd, **kw):
        if cmd[:1] == ["git"]:
            raise OSError("no git in test")
        clip.append((cmd, kw.get("input")))

        class P:
            returncode = 0

        return P()

    t = {"now": 0.0}

    def sleep(s):
        t["now"] += s
        # the owner clicks Load unpacked after ~6 s
        if t["now"] >= 6:
            cur = tmp_path / "awc" / "current"
            (ud / "Default" / "Secure Preferences").write_text(
                json.dumps(_ext({"location": 4, "path": str(cur)}))
            )

    res = aw.install(
        env=env,
        browsers=[br],
        fetch=lambda u: (_ for _ in ()).throw(OSError("offline")),
        run=run,
        popen=lambda cmd, **kw: launched.append(cmd),
        sleep=sleep,
        clock=lambda: t["now"],
        log=lambda s: None,
        wait=30,
    )
    assert launched == [["C:/chrome.exe", "chrome://extensions"]]
    assert clip and clip[0][1] == str(tmp_path / "awc" / "current").encode()
    assert res["clipboard"] is True and res["opened"] is True
    assert len(res["steps"]) == 2 and "Developer mode" in res["steps"][0]
    assert "Load unpacked" in res["steps"][1]
    assert res["status"]["state"] == "installed"


def test_install_with_no_source_refuses(tmp_path):
    env = {**_env(tmp_path), "AITHEROS_ROOT": str(tmp_path / "nowhere")}

    def fetch(u):
        raise OSError("404")

    with pytest.raises(aw.AwconnectError):
        aw.install(env=env, prefer="release", browsers=[], fetch=fetch, log=lambda s: None, wait=0)


def test_detect_browsers_windows_paths(tmp_path):
    env = {"ProgramFiles": "C:/PF", "ProgramFiles(x86)": "C:/PF86", "LOCALAPPDATA": "C:/LA"}
    present = {
        str(Path("C:/PF86") / "Microsoft/Edge/Application/msedge.exe"),
        str(Path("C:/LA") / "Microsoft/Edge/User Data"),
    }
    found = aw.detect_browsers(
        "win32", env, tmp_path, exists=lambda p: p in present, which=lambda n: None
    )
    assert [b.id for b in found] == ["edge"]
    assert found[0].extensions_url == "edge://extensions"


def test_cli_registers_awconnect():
    import argparse

    from adk.awconnect_setup import register_parser

    p = argparse.ArgumentParser()
    register_parser(p.add_subparsers(dest="command"))
    ns = p.parse_args(["awconnect", "install", "--update", "--json"])
    assert ns.command == "awconnect" and ns.awconnect_action == "install" and ns.update


def test_stage_allowlists_the_staged_copy_on_the_daemon(tmp_path):
    from adk.extension_id import unpacked_extension_id
    blob = _zip_bytes("3.9.0")
    fetch = _fake_release("3.9.0", blob, hashlib.sha256(blob).hexdigest())
    staged = aw.stage(aw.latest_release(fetch), _env(tmp_path), fetch=fetch)
    expected = unpacked_extension_id(tmp_path / "awc" / "current")
    assert staged["extension_id"] == expected
    allow = tmp_path / "home" / "awconnect" / "allowed_extension_ids"
    assert allow.read_text(encoding="utf-8").split() == [expected]
    aw.stage(aw.latest_release(fetch), _env(tmp_path), fetch=fetch)   # re-stage: no duplicate
    assert allow.read_text(encoding="utf-8").split() == [expected]
