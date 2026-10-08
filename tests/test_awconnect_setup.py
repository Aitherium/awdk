"""adk awconnect -- status from real-shaped browser profiles, source choice, and the
checksum refusal.

What must hold and is not obvious from the source:
  - status reads BOTH Preferences and Secure Preferences (Chrome moved unpacked
    extensions into the latter) and names installed / disabled / absent / stale;
  - a release asset whose bytes do not hash to its .sha256 is REFUSED, and a
    release with no .sha256 at all is never a candidate;
  - a strictly newer checkout beats an older release (no silent downgrade);
  - a checkout source is awconnect-next BUILT in a temp tree (never the shared
    tree, never the retired repo-root ``awconnect/`` v3 folder), and a failed
    build refuses with the Chrome Web Store link;
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


def _fake_release(version: str, blob: bytes, sha: str | None, variant: str = "unpacked"):
    name = f"aither-connect-{variant}-v{version}.zip"
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


def _next_repo(tmp_path: Path, version: str, dist_version: str | None) -> Path:
    """A monorepo with the 4.x source (and, optionally, a built dist/) beside a
    legacy awconnect/ tree that must never be staged."""
    repo = tmp_path / "repo"
    nxt = repo / "AitherOS" / "apps" / "awconnect-next"
    (nxt / "public").mkdir(parents=True)
    (nxt / "package.json").write_text(json.dumps({"name": "awconnect-next", "version": version}))
    (nxt / "public" / "manifest.json").write_text(
        json.dumps({"manifest_version": 3, "name": "awconnect", "version": version})
    )
    (nxt / "src").mkdir()
    (nxt / "src" / "main.ts").write_text("// source, not loadable")
    if dist_version is not None:
        _write_dist(nxt, dist_version)
    legacy = repo / "awconnect"
    legacy.mkdir()
    (legacy / "manifest.json").write_text(
        json.dumps({"manifest_version": 3, "name": "AitherConnect", "version": "99.0.0"})
    )
    (legacy / "LEGACY_MARK.js").write_text("legacy")
    return repo


def _write_dist(nxt: Path, version: str) -> None:
    dist = nxt / "dist"
    (dist / "assets").mkdir(parents=True, exist_ok=True)
    (dist / "manifest.json").write_text(
        json.dumps({"manifest_version": 3, "name": "awconnect", "version": version})
    )
    (dist / "background.js").write_text("// built sw")
    (dist / "assets" / "app.js").write_text("// built")
    (dist / "stray.pem").write_text("never ship a key")


def _no_tools(name):
    return None


def test_find_checkout_is_the_4x_tree_never_legacy(tmp_path):
    repo = _next_repo(tmp_path, "4.1.5", "4.1.5")
    nxt = repo / "AitherOS" / "apps" / "awconnect-next"
    assert aw.find_checkout({"AITHEROS_ROOT": str(repo)}) == nxt
    # AITHER_AWCONNECT_REPO may name the AitherOS/ subdir or the source dir itself.
    assert aw.find_checkout({"AITHER_AWCONNECT_REPO": str(repo / "AitherOS")}) == nxt
    assert aw.find_checkout({"AITHER_AWCONNECT_REPO": str(nxt)}) == nxt
    cand = aw.checkout_candidate(nxt)
    assert cand is not None and cand.version == "4.1.5"
    # A tree with ONLY the legacy awconnect/ is no checkout source at all.
    only_legacy = tmp_path / "old"
    (only_legacy / "awconnect").mkdir(parents=True)
    (only_legacy / "awconnect" / "manifest.json").write_text(
        json.dumps({"manifest_version": 3, "name": "awconnect", "version": "3.8.0"})
    )
    assert aw._next_source(only_legacy) is None


def test_checkout_stages_built_dist_not_legacy_tree(tmp_path):
    repo = _next_repo(tmp_path, "4.1.5", "4.1.5")
    cand = aw.checkout_candidate(aw.find_checkout({"AITHEROS_ROOT": str(repo)}))

    def no_run(*a, **k):
        raise AssertionError(f"an up-to-date dist/ must not be rebuilt: {a}")

    staged = aw.stage(cand, _env(tmp_path), run=no_run)
    cur = Path(staged["path"])
    assert staged["method"] == "dist" and staged["version"] == "4.1.5"
    assert (cur / "background.js").is_file() and (cur / "assets" / "app.js").is_file()
    assert not (cur / "LEGACY_MARK.js").exists() and not (cur / "src").exists()
    assert not (cur / "stray.pem").exists()


def test_checkout_without_dist_builds_with_npm(tmp_path):
    repo = _next_repo(tmp_path, "4.1.5", None)
    nxt = repo / "AitherOS" / "apps" / "awconnect-next"
    cand = aw.checkout_candidate(nxt)
    calls = []

    class Done:
        returncode = 0
        stdout = b""
        stderr = b""

    def run(cmd, **kw):
        if cmd[:1] == ["git"]:
            raise OSError("no git in test")  # -> working-tree copy of the app
        calls.append((cmd[1:], kw.get("cwd")))
        if cmd[1:] == ["run", "build"]:
            _write_dist(Path(kw["cwd"]), "4.1.5")
        return Done()

    dest = tmp_path / "out"
    meta = aw.stage_checkout(cand, dest, run=run, which=lambda n: f"/usr/bin/{n}")
    assert [c for c, _ in calls] == [["ci"], ["run", "build"]]
    # Built in a temp tree, never in the shared checkout (no dist/ appears there).
    assert all(repo.resolve() not in Path(cwd).resolve().parents for _, cwd in calls)
    assert not (nxt / "dist").exists()
    assert meta["method"] == "copy+npm-build" and meta["dist_version"] == "4.1.5"
    assert (dest / "manifest.json").is_file() and (dest / "background.js").is_file()


def test_checkout_build_failure_is_a_clear_refusal(tmp_path):
    repo = _next_repo(tmp_path, "4.1.5", None)
    cand = aw.checkout_candidate(repo / "AitherOS" / "apps" / "awconnect-next")

    class Failed:
        returncode = 1
        stdout = b""
        stderr = b"npm ERR! missing script"

    with pytest.raises(aw.AwconnectError) as exc:
        aw.stage_checkout(cand, tmp_path / "out", run=lambda c, **k: Failed(),
                          which=lambda n: f"/usr/bin/{n}")
    msg = str(exc.value)
    assert "npm ci" in msg and "exit 1" in msg and "missing script" in msg
    assert aw.WEBSTORE_URL in msg and aw.RELEASES_PAGE in msg


def test_checkout_without_dist_or_node_names_release_and_store(tmp_path):
    repo = _next_repo(tmp_path, "4.1.5", None)
    cand = aw.checkout_candidate(repo / "AitherOS" / "apps" / "awconnect-next")
    with pytest.raises(aw.AwconnectError) as exc:
        aw.stage_checkout(cand, tmp_path / "out", which=_no_tools)
    msg = str(exc.value)
    assert "node/npm is not on PATH" in msg
    assert aw.WEBSTORE_URL in msg and aw.RELEASES_PAGE in msg
    assert not (tmp_path / "out" / "LEGACY_MARK.js").exists()


def test_checkout_stale_dist_without_node_is_staged_as_its_own_version(tmp_path):
    repo = _next_repo(tmp_path, "4.1.5", "4.1.4")
    cand = aw.checkout_candidate(repo / "AitherOS" / "apps" / "awconnect-next")
    meta = aw.stage_checkout(cand, tmp_path / "out", which=_no_tools)
    assert meta["method"] == "dist-stale" and meta["dist_version"] == "4.1.4"


def test_checkout_stale_dist_older_than_the_release_is_refused(tmp_path):
    repo = _next_repo(tmp_path, "4.1.6", "4.0.0")
    cand = aw.checkout_candidate(repo / "AitherOS" / "apps" / "awconnect-next")
    with pytest.raises(aw.AwconnectError) as exc:
        aw.stage_checkout(cand, tmp_path / "out", which=_no_tools, min_version="4.1.5")
    assert "older than release 4.1.5" in str(exc.value)
    # Not older than the floor: the stale dist is still better than nothing.
    meta = aw.stage_checkout(cand, tmp_path / "out2", which=_no_tools, min_version="4.0.0")
    assert meta["method"] == "dist-stale"


def _install_quiet(env, fetch, run, logs):
    return aw.install(env=env, browsers=[], fetch=fetch, run=run, log=logs.append,
                      wait=0, open_browser=False, update_only=True)


def test_install_falls_back_to_release_when_checkout_dist_is_stale(tmp_path, monkeypatch):
    """Release 4.1.5, source 4.1.6 (develop right after a bump), dist 4.0.0, no
    npm: the checkout wins choose_source but must NOT stage 4.0.0 -- the release
    is staged instead."""
    repo = _next_repo(tmp_path, "4.1.6", "4.0.0")
    env = {**_env(tmp_path), "AITHEROS_ROOT": str(repo)}
    blob = _zip_bytes("4.1.5")
    fetch = _fake_release("4.1.5", blob, hashlib.sha256(blob).hexdigest())
    monkeypatch.setattr(aw.shutil, "which", _no_tools)

    def run(cmd, **kw):
        raise AssertionError(f"nothing may run without npm: {cmd}")

    logs: list = []
    res = _install_quiet(env, fetch, run, logs)
    assert res["source"] == "release" and res["staged"]["version"] == "4.1.5"
    assert not (tmp_path / "awc" / "4.0.0").exists()
    assert any("falling back to release 4.1.5" in line for line in logs)


def test_install_falls_back_to_release_when_checkout_build_fails(tmp_path, monkeypatch):
    repo = _next_repo(tmp_path, "4.1.6", None)
    env = {**_env(tmp_path), "AITHEROS_ROOT": str(repo)}
    blob = _zip_bytes("4.1.5")
    fetch = _fake_release("4.1.5", blob, hashlib.sha256(blob).hexdigest())
    monkeypatch.setattr(aw.shutil, "which", lambda n: f"/usr/bin/{n}")

    class Failed:
        returncode = 2
        stdout = b""
        stderr = b"error TS2322: typecheck failed"

    def run(cmd, **kw):
        if "npm" in str(cmd[0]):
            return Failed()
        if cmd[:1] == ["git"]:
            raise OSError("no git in test")
        raise AssertionError(f"unexpected command {cmd}")

    logs: list = []
    res = _install_quiet(env, fetch, run, logs)
    assert res["source"] == "release" and res["staged"]["version"] == "4.1.5"
    assert (tmp_path / "awc" / "current" / "background.js").is_file()


def test_install_without_a_release_still_surfaces_the_build_failure(tmp_path, monkeypatch):
    """No release to fall back to: the checkout's refusal reaches the user."""
    repo = _next_repo(tmp_path, "4.1.6", None)
    env = {**_env(tmp_path), "AITHEROS_ROOT": str(repo)}
    monkeypatch.setattr(aw.shutil, "which", _no_tools)
    with pytest.raises(aw.AwconnectError) as exc:
        _install_quiet(env, lambda u: (_ for _ in ()).throw(OSError("offline")),
                       lambda *a, **k: None, [])
    assert "node/npm is not on PATH" in str(exc.value)


def test_default_variant_is_the_one_build_connect_publishes():
    """build-connect.yml publishes -public- and -unpacked- zips only; the old
    default 'enterprise' was never built."""
    assert aw.DEFAULT_VARIANT == "unpacked"
    blob = _zip_bytes("4.1.5")
    sha = hashlib.sha256(blob).hexdigest()
    rel = aw.latest_release(_fake_release("4.1.5", blob, sha))
    assert rel is not None and rel.meta["asset"] == "aither-connect-unpacked-v4.1.5.zip"
    # enterprise stays selectable, and still falls back to unpacked when absent.
    rel = aw.latest_release(_fake_release("4.1.5", blob, sha), variant="enterprise")
    assert rel is not None and rel.meta["asset"] == "aither-connect-unpacked-v4.1.5.zip"
    import argparse

    p = argparse.ArgumentParser()
    aw.register_parser(p.add_subparsers(dest="command"))
    assert p.parse_args(["awconnect", "install"]).variant == "unpacked"
    for v in ("enterprise", "public"):
        assert p.parse_args(["awconnect", "install", "--variant", v]).variant == v


_REAL_APP = Path(__file__).resolve().parents[2] / "AitherOS" / "apps" / "awconnect-next"


# The extension source lives only in the monorepo; the published payload ships
# adk alone, so this test can only run where that tree exists.
@pytest.mark.skipif(not (_REAL_APP / "public" / "manifest.json").is_file(),
                    reason="needs the extension source tree (monorepo checkout only)")
def test_stage_reports_the_key_pinned_id_for_a_keyed_manifest(tmp_path):
    """The real 4.x manifest key must produce the pinned first-party id, and a
    keyed staged copy must report THAT id, not a path-derived one."""
    from adk.extension_id import PINNED_EXTENSION_ID, key_extension_id

    real = _REAL_APP
    key = json.loads((real / "public" / "manifest.json").read_text(encoding="utf-8"))["key"]
    assert key_extension_id(key) == PINNED_EXTENSION_ID
    src = tmp_path / "src"
    src.mkdir()
    (src / "manifest.json").write_text(
        json.dumps({"manifest_version": 3, "name": "awconnect", "version": "4.1.5", "key": key})
    )
    staged = aw.stage(aw.Candidate("dir", "4.1.5", str(src)), _env(tmp_path))
    assert staged["extension_id"] == PINNED_EXTENSION_ID


def _shared_repo(repo: Path, version: str = "4.1.5") -> Path:
    """A monorepo shaped like the real one: awconnect-next + awkit, with the shared
    tree's own node_modules/dist present (they must not reach the build tree)."""
    app = repo / aw.APP_REL
    (app / "public").mkdir(parents=True)
    (app / "public" / "manifest.json").write_text(
        json.dumps({"manifest_version": 3, "name": "awconnect", "version": version, "key": "K"})
    )
    (app / "package.json").write_text("{}")
    (app / "node_modules" / "dep").mkdir(parents=True)
    (app / "node_modules" / "dep" / "index.js").write_text("shared")
    (app / "dist").mkdir()
    (app / "dist" / "stale.js").write_text("stale")
    (repo / aw.AWKIT_REL).mkdir(parents=True)
    (repo / aw.AWKIT_REL / "package.json").write_text("{}")
    return repo


def _fake_build(calls: list, fail_on: str = ""):
    """A subprocess.run stand-in: no git, and an npm whose build writes dist/."""

    class P:
        def __init__(self, code: int = 0, err: bytes = b""):
            self.returncode, self.stderr, self.stdout = code, err, b""

    def run(cmd, **kw):
        calls.append((list(cmd), kw.get("cwd")))
        if cmd[:1] == ["git"]:
            raise OSError("no git in test")
        if kw.get("cwd"):  # an npm step
            app = Path(kw["cwd"])
            step = " ".join(cmd[1:])
            assert not (app / "node_modules").exists(), "shared node_modules leaked in"
            assert not (app / "dist").exists() or step != "ci", "shared dist leaked in"
            if step == fail_on:
                return P(1, b"npm ERR! network")
            if step == "run build":
                (app / "dist").mkdir()
                man = (app / "public" / "manifest.json").read_text()
                (app / "dist" / "manifest.json").write_text(man)
                (app / "dist" / "background.js").write_text("b")
                (app / "dist" / "key.pem").write_text("k")
            return P()
        return P()  # clipboard

    return run


def test_checkout_builds_awconnect_next_in_a_temp_tree(tmp_path, monkeypatch):
    monkeypatch.setattr(aw.shutil, "which", lambda n: f"/usr/bin/{n}")
    repo = _shared_repo(tmp_path / "repo")
    calls: list = []
    cand = aw.checkout_candidate(aw.find_checkout({"AITHEROS_ROOT": str(repo)}))
    assert cand.version == "4.1.5"
    staged = aw.stage(cand, _env(tmp_path), run=_fake_build(calls))
    cur = Path(staged["path"])
    assert json.loads((cur / "manifest.json").read_text())["key"] == "K"
    assert (cur / "background.js").is_file()
    assert not (cur / "key.pem").exists() and not (cur / "stale.js").exists()
    assert staged["method"] == "copy+npm-build" and staged["version"] == "4.1.5"
    npm = [(c[1:], cwd) for c, cwd in calls if cwd]
    assert [c for c, _ in npm] == [["ci"], ["run", "build"]]
    # Built outside the shared checkout, which keeps its own dist untouched.
    assert all(repo.resolve() not in Path(cwd).resolve().parents for _, cwd in npm)
    assert (repo / aw.APP_REL / "dist" / "stale.js").read_text() == "stale"


def test_checkout_build_failure_refuses_with_the_store_link(tmp_path, monkeypatch):
    monkeypatch.setattr(aw.shutil, "which", lambda n: f"/usr/bin/{n}")
    repo = _shared_repo(tmp_path / "repo")
    cand = aw.checkout_candidate(repo / aw.APP_REL)
    with pytest.raises(aw.AwconnectError, match="chromewebstore.google.com"):
        aw.stage(cand, _env(tmp_path), run=_fake_build([], fail_on="ci"))
    assert not (tmp_path / "awc" / "current" / "manifest.json").exists()


def test_retired_awconnect_tree_is_never_a_source(tmp_path, monkeypatch):
    # Gap: the fallback used to export the repo-root awconnect/ (v3) whenever the
    # private release lookup 404'd. A root holding only that tree is no checkout.
    # find_checkout also walks up from the module itself; detach it from the real
    # monorepo this test runs in.
    monkeypatch.setattr(aw, "__file__", str(tmp_path / "pkg" / "adk" / "awconnect_setup.py"))
    repo = tmp_path / "repo"
    (repo / "awconnect").mkdir(parents=True)
    (repo / "awconnect" / "manifest.json").write_text(
        json.dumps({"manifest_version": 3, "name": "awconnect", "version": "3.9.9"})
    )
    env = {**_env(tmp_path), "AITHEROS_ROOT": str(repo)}
    assert aw.find_checkout(env, start=repo) is None
    calls: list = []

    def fetch(u):
        raise OSError("404")

    with pytest.raises(aw.AwconnectError, match="chromewebstore.google.com"):
        aw.install(env=env, browsers=[], fetch=fetch, run=_fake_build(calls),
                   log=lambda s: None, wait=0)
    assert calls == []
    assert not (tmp_path / "awc" / "current").exists()
    # The module never names the retired folder as a path to read.
    monkeypatch.undo()
    src = Path(aw.__file__).read_text(encoding="utf-8")
    assert '/ "awconnect" / "manifest.json"' not in src
    assert 'root / "awconnect"' not in src


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
    repo = _next_repo(tmp_path, "4.1.5", "4.1.5")
    env = {**_env(tmp_path), "AITHEROS_ROOT": str(repo)}
    ud = tmp_path / "ud"
    (ud / "Default").mkdir(parents=True)
    (ud / "Default" / "Preferences").write_text("{}")
    br = aw.Browser("chrome", "Google Chrome", "C:/chrome.exe", ud)

    launched, clip, calls = [], [], []
    build = _fake_build(calls)

    def run(cmd, **kw):
        if "npm" in str(cmd[0]):
            raise AssertionError("an up-to-date dist/ must not be rebuilt")
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
    assert res["staged"]["version"] == "4.1.5" and res["source"] == "checkout"


def test_install_with_no_source_refuses(tmp_path):
    env = {**_env(tmp_path), "AITHEROS_ROOT": str(tmp_path / "nowhere")}

    def fetch(u):
        raise OSError("404")

    with pytest.raises(aw.AwconnectError) as exc:
        aw.install(env=env, prefer="release", browsers=[], fetch=fetch, log=lambda s: None, wait=0)
    assert aw.WEBSTORE_URL in str(exc.value) and aw.RELEASES_PAGE in str(exc.value)


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
