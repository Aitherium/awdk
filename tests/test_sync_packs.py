"""`adk sync packs` -- diff + apply against a fake HTTP layer.

Every test drives the real module through an injected ``http`` callable, so the
endpoints, headers, scoping and safety rules are exercised exactly as shipped.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import tarfile
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

import pytest

from adk.sync import packs as sp

PORTAL = "https://portal.test"
G = sp.GENESIS_PREFIX  # Genesis routes go through the portal's bridge


def _tarball(pack_id: str, version: str, files: Optional[Dict[str, bytes]] = None) -> bytes:
    buf = io.BytesIO()
    files = files or {"pack.yaml": f"id: {pack_id}\nversion: {version}\n".encode()}
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for rel, data in files.items():
            info = tarfile.TarInfo(f"{pack_id}-{version}/{rel}")
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class FakeHttp:
    """Routes (method, path) to canned responses and records every call."""

    def __init__(self) -> None:
        self.routes: Dict[Tuple[str, str], Tuple[int, Dict[str, str], bytes]] = {}
        self.calls: List[Tuple[str, str, Dict[str, str]]] = []
        self.down: set = set()

    def json(self, path: str, body: Any, status: int = 200) -> None:
        self.routes[("GET", path)] = (status, {}, json.dumps(body).encode())

    def tar(self, pack_id: str, version: str, *, sha: Optional[str] = None,
            status: int = 200, body: Optional[bytes] = None,
            stripped: bool = False) -> None:
        """``stripped`` = the portal bridge before 2026-10-04: it forwarded only
        Content-Type/Disposition/Length, dropping every integrity header."""
        data = body if body is not None else _tarball(pack_id, version)
        headers = {"X-Pack-Version": version,
                   "X-Pack-SHA256": sha or hashlib.sha256(data).hexdigest()}
        if stripped:
            headers = {"Content-Type": "application/gzip"}
        self.routes[("GET", f"{G}/v1/packs/{pack_id}/download")] = (status, headers, data)

    def __call__(self, method: str, url: str, headers: Mapping[str, str]):
        assert url.startswith(PORTAL)
        path = url[len(PORTAL):]
        self.calls.append((method, path, dict(headers)))
        if path in self.down:
            raise OSError("connection refused")
        if (method, path) not in self.routes:
            return 404, {}, b'{"detail":"not found"}'
        return self.routes[(method, path)]

    def paths(self) -> List[str]:
        return [p for _m, p, _h in self.calls]


def _cloud(http: FakeHttp, *, workspaces=None, licenses=None, catalog=None,
           bundle_ws: str = "", tenant_listed: Optional[bool] = None) -> None:
    http.json(sp.WORKSPACES_PATH, {"workspaces": workspaces if workspaces is not None
                                   else [{"id": "ws1", "name": "Acme"}]})
    http.json(G + sp.BUNDLE_PATH, {"role": "user", "identity": {"workspace_id": bundle_ws}})
    lic: Dict[str, Any] = {"licenses": licenses if licenses is not None else []}
    if tenant_listed is not None:  # None = an older Genesis that omits the field
        lic["tenant_listed"] = tenant_listed
    http.json(G + sp.LICENSES_PATH, lic)
    http.json(G + sp.CATALOG_PATH, {"packs": catalog if catalog is not None else []})


def _lic(pid: str, status: str = "active", **kw) -> Dict[str, Any]:
    return {"listing_id": pid, "status": status, **kw}


def _local_pack(root: Path, pid: str, version: str = "1.0.0", *, ledger_ws=None,
                entitlement: bool = False, source: str = "license") -> None:
    d = root / pid
    d.mkdir(parents=True)
    (d / "pack.yaml").write_text(f"id: {pid}\nversion: {version}\n", encoding="utf-8")
    if entitlement:
        (d / sp.ENTITLEMENT_FILE).write_text(json.dumps({"pack_id": pid}), encoding="utf-8")
    if ledger_ws is not None:
        led = sp.load_ledger(root)
        led[pid] = {"version": version, "workspace": ledger_ws, "source": source}
        sp.save_ledger(root, led)


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    r = tmp_path / "packs"
    r.mkdir()
    return r


@pytest.fixture(autouse=True)
def _unsigned_ok(monkeypatch):
    # The verifier's policy decides on an absent signature; pin it permissive here
    # (signature failure has its own test below).
    monkeypatch.setenv("AITHER_PACK_REQUIRE_SIGNING", "false")


def _run(http, root, **kw):
    kw.setdefault("activate", lambda: {"status": "ok", "message": "reloaded"})
    return sp.run(http=http, portal=PORTAL, token="tok-123", root=root,
                  hooks=(None, None), **kw)


# ── diff ──────────────────────────────────────────────────────────────────────


class TestDiff:
    def test_four_buckets_and_kinds(self, root):
        http = FakeHttp()
        _cloud(http, licenses=[_lic("newpack"), _lic("oldpack"), _lic("samepack"),
                               _lic("expired", status="revoked")],
               catalog=[{"id": "newpack", "version": "1.0.0", "app_manifest_id": "newpack"},
                        {"id": "oldpack", "version": "2.0.0", "tool_count": 3},
                        {"id": "samepack", "version": "1.0.0", "skills": ["s"]}])
        _local_pack(root, "oldpack", "1.0.0", ledger_ws="ws1")
        _local_pack(root, "samepack", "1.0.0", ledger_ws="ws1")
        _local_pack(root, "gonepack", "1.0.0", ledger_ws="ws1")
        _local_pack(root, "handmade", "0.1.0")
        rep = _run(http, root, dry_run=True)
        assert rep["ok"] is True
        plan = rep["plan"]
        assert [i["id"] for i in plan["install"]] == ["newpack"]
        assert plan["install"][0]["kind"] == "app"
        assert [i["id"] for i in plan["update"]] == ["oldpack"]
        assert plan["update"][0]["kind"] == "tool"
        assert plan["update"][0]["reason"] == "1.0.0 -> 2.0.0"
        assert [i["id"] for i in plan["unchanged"]] == ["samepack"]
        assert plan["unchanged"][0]["kind"] == "skill"
        assert [i["id"] for i in plan["remove"]] == ["gonepack"]
        assert [i["id"] for i in plan["unmanaged"]] == ["handmade"]
        assert rep["counts"] == {"install": 1, "update": 1, "remove": 1,
                                 "unchanged": 1, "unmanaged": 1}

    def test_dry_run_changes_nothing(self, root):
        http = FakeHttp()
        _cloud(http, licenses=[_lic("newpack")])
        http.tar("newpack", "1.0.0")
        _local_pack(root, "gonepack", ledger_ws="ws1")
        rep = _run(http, root, dry_run=True)
        assert "results" not in rep
        assert not (root / "newpack").exists()
        assert (root / "gonepack").exists()
        assert not any("/download" in p for p in http.paths())

    def test_semver_not_string_order(self, root):
        http = FakeHttp()
        _cloud(http, licenses=[_lic("p")], catalog=[{"id": "p", "version": "0.10.0"}])
        _local_pack(root, "p", "0.9.0", ledger_ws="ws1")
        assert [i["id"] for i in _run(http, root, dry_run=True)["plan"]["update"]] == ["p"]

    def test_unknown_remote_version_is_unchanged_not_update(self, root):
        http = FakeHttp()
        _cloud(http, licenses=[_lic("p")], catalog=[])
        _local_pack(root, "p", "1.0.0", ledger_ws="ws1")
        plan = _run(http, root, dry_run=True)["plan"]
        assert plan["update"] == []
        assert plan["unchanged"][0]["reason"] == "present (version not comparable)"

    def test_other_workspace_packs_are_never_removed(self, root):
        http = FakeHttp()
        _cloud(http, workspaces=[{"id": "ws1"}, {"id": "ws2"}], licenses=[])
        _local_pack(root, "fromws1", ledger_ws="ws1")
        rep = _run(http, root, dry_run=True, workspace="ws2")
        assert rep["plan"]["remove"] == []
        assert [i["id"] for i in rep["plan"]["unmanaged"]] == ["fromws1"]

    def test_legacy_entitlement_pack_is_managed_at_account_scope(self, root):
        http = FakeHttp()
        # Two workspaces, no choice -> account scope, where `adk pack sync` installed.
        _cloud(http, workspaces=[{"id": "ws1"}, {"id": "ws2"}], licenses=[])
        _local_pack(root, "legacy", entitlement=True)
        rep = _run(http, root, dry_run=True)
        assert rep["workspace"]["source"] == "account"
        assert [i["id"] for i in rep["plan"]["remove"]] == ["legacy"]

    def test_no_remove_flag_keeps_ended_packs(self, root):
        http = FakeHttp()
        _cloud(http, licenses=[])
        _local_pack(root, "gonepack", ledger_ws="ws1")
        plan = _run(http, root, dry_run=True, allow_remove=False)["plan"]
        assert plan["remove"] == []
        assert plan["unchanged"][0]["id"] == "gonepack"


# ── scoping + auth ────────────────────────────────────────────────────────────


class TestScope:
    def test_single_workspace_is_auto_selected_and_sent_as_header(self, root):
        http = FakeHttp()
        _cloud(http)
        rep = _run(http, root, dry_run=True)
        assert rep["workspace"] == {"id": "ws1", "name": "Acme", "source": "only"}
        lic_calls = [h for _m, p, h in http.calls if p == G + sp.LICENSES_PATH]
        assert lic_calls[0]["X-Workspace-ID"] == "ws1"
        assert lic_calls[0]["Authorization"] == "Bearer tok-123"

    def test_bundle_workspace_wins_over_list(self, root):
        http = FakeHttp()
        _cloud(http, workspaces=[{"id": "ws1"}, {"id": "ws2"}], bundle_ws="ws2")
        assert _run(http, root, dry_run=True)["workspace"]["id"] == "ws2"

    def test_unknown_workspace_flag_is_refused(self, root):
        http = FakeHttp()
        _cloud(http)
        rep = _run(http, root, dry_run=True, workspace="nope")
        assert rep["ok"] is False
        assert "not one of yours" in rep["errors"][0]
        assert G + sp.LICENSES_PATH not in http.paths()

    def test_entitlement_outage_computes_no_removals(self, root):
        http = FakeHttp()
        _cloud(http)
        http.down.add(G + sp.LICENSES_PATH)
        _local_pack(root, "keepme", ledger_ws="ws1")
        rep = _run(http, root)
        assert rep["ok"] is False
        assert "plan" not in rep
        assert (root / "keepme").exists()

    def test_entitlement_401_computes_no_removals(self, root):
        http = FakeHttp()
        _cloud(http)
        http.json(G + sp.LICENSES_PATH, {"detail": "no"}, status=401)
        _local_pack(root, "keepme", ledger_ws="ws1")
        rep = _run(http, root)
        assert rep["ok"] is False and (root / "keepme").exists()
        assert any("HTTP 401" in e for e in rep["errors"])

    def test_signed_out_makes_no_request(self, root, monkeypatch):
        monkeypatch.setattr(sp, "resolve_token", lambda: ("", ""))
        http = FakeHttp()
        rep = sp.run(http=http, portal=PORTAL, root=root, hooks=(None, None))
        assert rep["ok"] is False and http.calls == []
        assert "not signed in" in rep["errors"][0]

    def test_token_never_in_report_or_url(self, root):
        http = FakeHttp()
        _cloud(http, licenses=[_lic("p")])
        http.tar("p", "1.0.0")
        rep = _run(http, root)
        assert "tok-123" not in json.dumps(rep)
        assert all("tok-123" not in p for p in http.paths())

    def test_genesis_routes_go_through_the_bridge_with_the_bearer(self, root):
        http = FakeHttp()
        _cloud(http, licenses=[_lic("p")])
        _run(http, root, dry_run=True)
        hdr = {p: h for _m, p, h in http.calls}
        # Measured live: the bare /v1/... path on the portal is a 404 HTML page,
        # and the bridge refuses an anonymous catalog read with 401.
        assert G + sp.LICENSES_PATH in hdr and sp.LICENSES_PATH not in hdr
        assert hdr[G + sp.CATALOG_PATH]["Authorization"] == "Bearer tok-123"

    def test_in_network_genesis_prefix_can_be_empty(self, root):
        http = FakeHttp()
        http.json(sp.WORKSPACES_PATH, {"workspaces": [{"id": "ws1"}]})
        http.json(sp.LICENSES_PATH, {"licenses": [_lic("p")]})
        rep = sp.run(http=http, portal=PORTAL, token="t", root=root, genesis="",
                     hooks=(None, None), dry_run=True)
        assert rep["ok"] and rep["plan"]["install"][0]["id"] == "p"

    def test_agents_listed_for_the_workspace(self, root):
        http = FakeHttp()
        _cloud(http)
        http.json("/api/workspaces/ws1/agents", {"agents": [{"id": "a1", "name": "Ava"}]})
        assert _run(http, root, dry_run=True)["agents"] == [{"id": "a1", "name": "Ava"}]


# ── apply ─────────────────────────────────────────────────────────────────────


class TestApply:
    def test_install_update_remove_and_ledger(self, root):
        http = FakeHttp()
        _cloud(http, licenses=[_lic("newpack"), _lic("oldpack")],
               catalog=[{"id": "newpack", "version": "1.0.0"},
                        {"id": "oldpack", "version": "2.0.0"}])
        http.tar("newpack", "1.0.0")
        http.tar("oldpack", "2.0.0")
        _local_pack(root, "oldpack", "1.0.0", ledger_ws="ws1")
        _local_pack(root, "gonepack", ledger_ws="ws1")
        _local_pack(root, "handmade")
        activated = []
        rep = _run(http, root, activate=lambda: activated.append(1) or {"status": "ok"})
        assert rep["ok"] is True, rep
        assert {(r["id"], r["action"], r["ok"]) for r in rep["results"]} == {
            ("newpack", "install", True), ("oldpack", "update", True),
            ("gonepack", "remove", True)}
        assert (root / "newpack" / "pack.yaml").is_file()
        assert "version: 2.0.0" in (root / "oldpack" / "pack.yaml").read_text()
        assert not (root / "gonepack").exists()
        assert (root / "handmade").exists()
        led = sp.load_ledger(root)
        assert led["newpack"]["version"] == "1.0.0" and led["newpack"]["workspace"] == "ws1"
        assert led["oldpack"]["version"] == "2.0.0"
        assert "gonepack" not in led
        assert (root / "newpack" / sp.ENTITLEMENT_FILE).is_file()
        assert activated == [1]
        # The server reload only ADDS: removed/updated packs are not unloaded,
        # so the report must not call this a clean reload.
        assert rep["activation"]["status"] == "partial"
        assert rep["activation"]["pending_restart"] == ["gonepack", "oldpack"]
        assert {r["id"]: r["integrity"] for r in rep["results"]
                if r["action"] != "remove"} == {"newpack": "sha256", "oldpack": "sha256"}
        dl = [h for _m, p, h in http.calls if p.endswith("/download")]
        assert all(h["X-Workspace-ID"] == "ws1" for h in dl)

    def test_second_run_is_a_no_op(self, root):
        http = FakeHttp()
        _cloud(http, licenses=[_lic("p")], catalog=[{"id": "p", "version": "1.0.0"}])
        http.tar("p", "1.0.0")
        assert _run(http, root)["ok"] is True
        activated = []
        rep = _run(http, root, activate=lambda: activated.append(1) or {})
        assert rep["counts"]["unchanged"] == 1 and rep["results"] == []
        assert activated == []  # nothing changed -> no reload

    def test_sha_mismatch_refuses_and_keeps_old(self, root):
        http = FakeHttp()
        _cloud(http, licenses=[_lic("p")], catalog=[{"id": "p", "version": "2.0.0"}])
        http.tar("p", "2.0.0", sha="0" * 64)
        _local_pack(root, "p", "1.0.0", ledger_ws="ws1")
        rep = _run(http, root)
        assert rep["ok"] is False
        assert rep["results"][0]["detail"] == "sha256 mismatch: refused"
        assert "version: 1.0.0" in (root / "p" / "pack.yaml").read_text()
        assert sp.load_ledger(root)["p"]["version"] == "1.0.0"

    def test_bad_signature_refuses(self, root, monkeypatch):
        import adk.pack_verifier as pv

        monkeypatch.setattr(pv, "verify_pack_tarball", lambda b, s: (False, "bad sig"))
        http = FakeHttp()
        _cloud(http, licenses=[_lic("p")])
        http.tar("p", "1.0.0")
        rep = _run(http, root)
        assert rep["ok"] is False and "signature check failed" in rep["results"][0]["detail"]
        assert not (root / "p").exists()

    def test_path_traversal_archive_refused(self, root):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as t:
            data = b"x"
            info = tarfile.TarInfo("../../escape.txt")
            info.size = 1
            t.addfile(info, io.BytesIO(data))
        http = FakeHttp()
        _cloud(http, licenses=[_lic("p")])
        http.tar("p", "1.0.0", body=buf.getvalue())
        rep = _run(http, root)
        assert rep["ok"] is False and "extract failed" in rep["results"][0]["detail"]
        assert not (root.parent.parent / "escape.txt").exists()
        assert not (root / "p").exists()

    def test_402_reports_license_and_continues(self, root):
        http = FakeHttp()
        _cloud(http, licenses=[_lic("a"), _lic("b")])
        http.tar("a", "1.0.0", status=402)
        http.tar("b", "1.0.0")
        rep = _run(http, root)
        by = {r["id"]: r for r in rep["results"]}
        assert by["a"]["ok"] is False and "license" in by["a"]["detail"]
        assert by["b"]["ok"] is True and (root / "b").is_dir()
        assert rep["ok"] is False

    def test_unsafe_listing_id_is_never_a_path(self, root):
        http = FakeHttp()
        _cloud(http, licenses=[_lic("../../etc"), _lic("ok-pack")])
        rep = _run(http, root, dry_run=True)
        assert [i["id"] for i in rep["plan"]["install"]] == ["ok-pack"]

    def test_credential_hooks_revoke_on_update_and_remove(self, root):
        http = FakeHttp()
        _cloud(http, licenses=[_lic("p")], catalog=[{"id": "p", "version": "2.0.0"}])
        http.tar("p", "2.0.0")
        _local_pack(root, "p", "1.0.0", ledger_ws="ws1")
        _local_pack(root, "gone", ledger_ws="ws1")
        minted, revoked = [], []
        rep = sp.run(http=http, portal=PORTAL, token="t", root=root,
                     hooks=(minted.append, revoked.append), activate=lambda: {})
        assert rep["ok"] is True
        assert minted == ["p"] and sorted(revoked) == ["gone", "p"]


# ── CLI wiring ────────────────────────────────────────────────────────────────


class TestCli:
    def test_sync_packs_is_registered_with_its_flags(self):
        from adk.cli import get_parser

        ns = get_parser().parse_args(
            ["sync", "packs", "--dry-run", "--json", "--workspace", "ws1", "--no-remove"])
        assert ns.command == "sync" and ns.sync_action == "packs"
        assert ns.dry_run and ns.json_output and ns.workspace == "ws1" and ns.no_remove

    def test_cmd_sync_dispatches_and_prints_json(self, root, monkeypatch, capsys):
        from adk import cli

        http = FakeHttp()
        _cloud(http, licenses=[_lic("p")])
        real_run = sp.run
        monkeypatch.setattr(sp, "run", lambda **kw: real_run(
            http=http, portal=PORTAL, token="t", root=root, hooks=(None, None), **kw))
        ns = argparse.Namespace(command="sync", sync_action="packs", dry_run=True,
                                json_output=True, workspace="", no_remove=False,
                                no_activate=True)
        assert cli.cmd_sync(ns) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["plan"]["install"][0]["id"] == "p" and out["dry_run"] is True

    def test_human_render_names_every_bucket(self, root):
        http = FakeHttp()
        _cloud(http, licenses=[_lic("p")])
        _local_pack(root, "hand")
        text = sp.render(_run(http, root, dry_run=True))
        assert "+ [agent] p" in text and "? hand" in text and "workspace: Acme" in text


# ── review fixes (PR #11726): integrity, tenant fence, versions, reload ───────


class TestIntegrity:
    def test_stripped_headers_fail_closed(self, root):
        http = FakeHttp()
        _cloud(http, licenses=[_lic("p")])
        http.tar("p", "1.0.0", stripped=True)
        rep = _run(http, root)
        assert rep["ok"] is False
        r = rep["results"][0]
        assert r["ok"] is False and r["integrity"] == "unverified"
        assert "refused" in r["detail"]
        assert not (root / "p").exists()
        assert "p" not in sp.load_ledger(root)

    def test_stripped_headers_install_only_with_explicit_override(self, root):
        http = FakeHttp()
        _cloud(http, licenses=[_lic("p")])
        http.tar("p", "1.0.0", stripped=True)
        rep = _run(http, root, allow_unverified=True)
        assert rep["ok"] is True
        assert rep["results"][0]["integrity"] == "unverified"
        assert sp.load_ledger(root)["p"]["integrity"] == "unverified"

    def test_signature_alone_counts_as_verified(self, root, monkeypatch):
        import adk.pack_verifier as pv

        seen = []
        monkeypatch.setattr(pv, "verify_pack_tarball",
                            lambda b, s: seen.append(s) or (True, "Pack signature verified"))
        http = FakeHttp()
        _cloud(http, licenses=[_lic("p")])
        http.tar("p", "1.0.0", stripped=True)
        key = ("GET", f"{G}/v1/packs/p/download")
        st, h, b = http.routes[key]
        http.routes[key] = (st, {**h, "X-Aither-Pack-Signature": "ab" * 32}, b)
        rep = _run(http, root)
        assert rep["ok"] is True and rep["results"][0]["integrity"] == "signature"
        assert seen == ["ab" * 32]

    def test_downloaded_version_header_wins_in_the_ledger(self, root):
        http = FakeHttp()
        _cloud(http, licenses=[_lic("p", version="1.0.0")])
        http.tar("p", "1.0.1")  # X-Pack-Version: what was actually served
        assert _run(http, root)["ok"] is True
        assert sp.load_ledger(root)["p"]["version"] == "1.0.1"

    def test_allow_unverified_flag_is_registered(self):
        from adk.cli import get_parser

        ns = get_parser().parse_args(["sync", "packs", "--allow-unverified"])
        assert ns.allow_unverified is True


class TestTenantFence:
    def _remote(self, tenant_listed):
        return sp.RemoteState(ok=True, workspace_id="ws1", entitled={},
                              tenant_listed=tenant_listed)

    @pytest.mark.parametrize("listed", [None, False])
    def test_unconfirmed_tenant_list_never_removes_a_company_pack(self, listed):
        # The reviewer's repro: entitled={} + a managed tenant pack in scope.
        local = {"garg": {"version": "1.0.0", "managed": True, "scope": "ws1",
                          "source": "tenant"}}
        plan = sp.compute_diff(self._remote(listed), local)
        assert plan.remove == []
        assert plan.unchanged[0].id == "garg"
        assert "unconfirmed" in plan.unchanged[0].reason

    def test_confirmed_tenant_list_removes_a_revoked_company_pack(self):
        local = {"garg": {"version": "1.0.0", "managed": True, "scope": "ws1",
                          "source": "tenant"}}
        assert [i.id for i in sp.compute_diff(self._remote(True), local).remove] == ["garg"]

    def test_purchased_packs_still_removed_without_the_flag(self):
        local = {"bought": {"version": "1.0.0", "managed": True, "scope": "ws1",
                            "source": "license"}}
        assert [i.id for i in sp.compute_diff(self._remote(None), local).remove] == ["bought"]

    def test_end_to_end_catalog_hiccup_keeps_the_persona_on_disk(self, root):
        http = FakeHttp()
        # Genesis' catalog read failed: 200, purchased rows only, tenant_listed false.
        _cloud(http, licenses=[], tenant_listed=False)
        _local_pack(root, "garg", ledger_ws="ws1", source="tenant")
        rep = _run(http, root)
        assert rep["plan"]["remove"] == [] and (root / "garg" / "pack.yaml").is_file()
        assert rep["tenant_listed"] is False
        assert any("tenant packs not confirmed" in e for e in rep["errors"])

    def test_end_to_end_confirmed_absence_removes(self, root):
        http = FakeHttp()
        _cloud(http, licenses=[], tenant_listed=True)
        _local_pack(root, "garg", ledger_ws="ws1", source="tenant")
        rep = _run(http, root)
        assert [r["id"] for r in rep["results"]] == ["garg"]
        assert not (root / "garg").exists()


class TestVersionsAndKinds:
    def test_license_row_version_drives_updates_for_agent_packs(self, root):
        # Brain/agent packs live only in packs_catalog.yaml, not /v1/packs/catalog.
        http = FakeHttp()
        _cloud(http, licenses=[_lic("garg", source="tenant", version="2.0.0",
                                    pack_type="agent_pack")],
               catalog=[], tenant_listed=True)
        _local_pack(root, "garg", "1.0.0", ledger_ws="ws1", source="tenant")
        plan = _run(http, root, dry_run=True)["plan"]
        assert [(i["id"], i["kind"], i["reason"]) for i in plan["update"]] == [
            ("garg", "agent", "1.0.0 -> 2.0.0")]

    def test_license_row_beats_the_tool_catalog(self, root):
        http = FakeHttp()
        _cloud(http, licenses=[_lic("p", version="3.0.0", pack_type="skill_pack")],
               catalog=[{"id": "p", "version": "9.9.9", "tool_count": 4}])
        item = _run(http, root, dry_run=True)["plan"]["install"][0]
        assert item["remote_version"] == "3.0.0" and item["kind"] == "skill"

    def test_tool_catalog_is_the_fallback_for_an_older_genesis(self, root):
        http = FakeHttp()
        _cloud(http, licenses=[_lic("p")], catalog=[{"id": "p", "version": "1.2.0",
                                                     "tool_count": 1}])
        item = _run(http, root, dry_run=True)["plan"]["install"][0]
        assert item["remote_version"] == "1.2.0" and item["kind"] == "tool"


class TestReloadHonesty:
    def test_install_only_keeps_ok_status(self, root):
        http = FakeHttp()
        _cloud(http, licenses=[_lic("p")])
        http.tar("p", "1.0.0")
        act = _run(http, root, activate=lambda: {"status": "ok"})["activation"]
        assert act == {"status": "ok", "pending_restart": []}

    def test_render_tells_the_user_to_restart_for_removals(self, root):
        http = FakeHttp()
        _cloud(http, licenses=[])
        _local_pack(root, "gonepack", ledger_ws="ws1")
        rep = _run(http, root, activate=lambda: {"status": "ok", "message": "reloaded"})
        text = sp.render(rep)
        assert "agent reload: partial" in text
        assert "restart the agent to unload: gonepack" in text
