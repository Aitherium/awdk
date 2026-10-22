"""adk discover: sources read what is on disk, the scope filter fails closed, ranking
reaches every domain, and the checks fail on a stale, empty or dead index.

No network: the gateway sources are fed a fake ``fetch``; HOME and the index directory
are temp dirs, so nothing here reads or writes the developer's real ~/.aither.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from adk.discovery import check, cli, index, rank, scope, sources


@pytest.fixture()
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    (h / ".claude" / "skills" / "video-masks").mkdir(parents=True)
    (h / ".claude" / "skills" / "video-masks" / "SKILL.md").write_text(
        "---\nname: video-masks\ndescription: Segment objects in video and export masks\n---\n"
        "body\n", encoding="utf-8")
    monkeypatch.setenv("AITHER_HOME_OVERRIDE", str(h))
    monkeypatch.setenv("AITHER_DISCOVERY_DIR", str(tmp_path / "idx"))
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.delenv("AITHER_SESSION_BEARER", raising=False)
    return h


@pytest.fixture()
def project(tmp_path):
    p = tmp_path / "proj"
    (p / ".claude" / "skills" / "safe-git").mkdir(parents=True)
    (p / ".claude" / "skills" / "safe-git" / "SKILL.md").write_text(
        "---\ndescription: Commit safely in a shared worktree\n---\n", encoding="utf-8")
    (p / ".claude" / "agents").mkdir()
    (p / ".claude" / "agents" / "hydra.md").write_text(
        "---\nname: hydra\ndescription: code review\n---\n", encoding="utf-8")
    (p / "awpack" / "packs" / "bead").mkdir(parents=True)
    (p / "awpack" / "packs" / "bead" / "pack.yaml").write_text(
        "name: bead\ndescription: beads\nlicense: AGPL-3.0\n", encoding="utf-8")
    return p


def test_file_sources_read_skills_agents_and_packs(home, project) -> None:
    cards, rec = sources.cwd_skills(project)
    assert rec["ok"] and [c["name"] for c in cards] == ["safe-git"]
    cards, rec = sources.user_skills()
    assert rec["ok"] and cards[0]["name"] == "video-masks"
    cards, _ = sources.cwd_agents(project)
    assert cards[0]["name"] == "hydra" and cards[0]["domain"] == "agents"
    cards, _ = sources.awpack_shelf(project)
    assert cards[0]["license_flag"] == "agpl"
    assert all(c["hosting"] in ("local", "fleet", "hosted") for c in cards)


def test_plugin_skills_follow_the_installed_registry_not_every_cached_version(home) -> None:
    base = home / ".claude" / "plugins"
    for ver in ("0.1.0", "0.2.0"):
        d = base / "cache" / "kit" / "kit" / ver / "skills" / f"s{ver[2]}"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text("---\ndescription: x\n---\n", encoding="utf-8")
    (base / "installed_plugins.json").write_text(json.dumps({"version": 2, "plugins": {
        "kit@kit": [{"scope": "user",
                     "installPath": str(base / "cache" / "kit" / "kit" / "0.2.0")}]}}),
        encoding="utf-8")
    cards, rec = sources.plugin_skills(Path("."))
    assert rec["ok"] and [c["name"] for c in cards] == ["kit:s2"]


def test_a_dead_gateway_is_a_receipt_not_silence(home) -> None:
    def boom(url, bearer=""):
        raise ConnectionRefusedError("refused")
    os.environ["AITHER_SESSION_BEARER"] = "test-bearer"
    try:
        cards, rec = sources.gateway_tools(fetch=boom)
    finally:
        os.environ.pop("AITHER_SESSION_BEARER", None)
    assert cards == [] and rec["ok"] is False and "refused" in rec["error"]
    cards, rec = sources.gateway_tools()
    assert rec["ok"] is False and "bearer" in rec["error"]


def test_gateway_tools_are_marked_hosted_or_fleet(home) -> None:
    doc = {"tools": [{"name": "rf_detect", "description": "detect objects"},
                     {"name": "sana_video", "description": "make video"}]}
    os.environ["AITHER_SESSION_BEARER"] = "test-bearer"
    try:
        cards, rec = sources.gateway_tools(fetch=lambda url, bearer="": doc)
    finally:
        os.environ.pop("AITHER_SESSION_BEARER", None)
    by = {c["name"]: c["hosting"] for c in cards}
    assert rec["ok"] and by == {"rf_detect": "hosted", "sana_video": "fleet"}


def test_scope_fails_closed() -> None:
    cards = [{"domain": "tools", "name": "fleet_control", "source": "gateway",
              "hosting": "fleet"},
             {"domain": "tools", "name": "federation_sync", "source": "gateway",
              "hosting": "fleet"},
             {"domain": "packs", "name": "big", "hosting": "hosted", "min_tier": "enterprise"},
             {"domain": "packs", "name": "nolic", "hosting": "local", "entitled": False},
             {"domain": "tools", "name": "nohost", "source": "gateway"},
             {"domain": "skills", "name": "ok", "hosting": "local"}]
    kept, dropped = scope.filter_cards(cards, scope.Caller())
    assert [c["name"] for c in kept] == ["ok"]
    assert dropped["platform-only"] == 1 and dropped["no hosting mark"] == 1
    plat = scope.caller_from_me({"tenant_id": "platform", "tier": "platform"})
    kept, _ = scope.filter_cards(cards, plat)
    assert {c["name"] for c in kept} == {"fleet_control", "big", "ok"}


def _table(cards):
    return [("fixture", lambda _cwd: ([dict(c) for c in cards], {"ok": True,
                                                                 "count": len(cards)}))]


def test_index_parts_are_keyed_by_cwd_and_written_atomically(home, project, tmp_path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    caller = scope.Caller(tenant_id="t", tier="free", known=True)
    a = index.build_part("cwd", project, caller=caller, table=_table(
        [{"domain": "skills", "name": "a", "hosting": "local", "one_line": "a"}]))
    b = index.build_part("cwd", other, caller=caller, table=_table(
        [{"domain": "skills", "name": "b", "hosting": "local", "one_line": "b"}]))
    assert index.part_path("cwd", project) != index.part_path("cwd", other)
    assert [c["name"] for c in a["cards"]] == ["a"] and [c["name"] for c in b["cards"]] == ["b"]
    assert not list((tmp_path / "idx").glob("*.tmp"))


def test_network_sources_are_reused_within_their_ttl(home, project) -> None:
    calls = []

    def src(_cwd):
        calls.append(1)
        return [{"domain": "tools", "name": "t", "hosting": "fleet", "source": "gateway"}], {
            "ok": True, "count": 1}
    caller = scope.Caller(tenant_id="t", tier="free", known=True)
    for _ in range(2):
        index.build_part("user", project, caller=caller, table=[("gateway_tools", src)])
    assert len(calls) == 1
    index.build_part("user", project, caller=caller, table=[("gateway_tools", src)], force=True)
    assert len(calls) == 2


FP = "f0f0f0f0f0f0"
CALLER = {"tenant_id": "t", "tier": "free", "known": True, "fingerprint": FP}


def _merged(now=None):
    t = time.time() if now is None else now
    cards = [
        {"domain": "tools", "name": "sana_video", "one_line": "segment objects in video masks",
         "hosting": "fleet", "source": "gateway", "how_to_invoke": "x", "fetched_for": FP},
        {"domain": "tools", "name": "web_search", "one_line": "search the web for a topic",
         "hosting": "hosted", "source": "gateway", "how_to_invoke": "x", "fetched_for": FP},
        {"domain": "tools", "name": "awgit", "one_line": "commit safely in a shared worktree",
         "hosting": "local", "source": "cli", "how_to_invoke": "x"},
        {"domain": "skills", "name": "video-masks", "one_line": "segment objects in video",
         "hosting": "local", "how_to_invoke": "x"},
        {"domain": "skills", "name": "rea", "one_line": "reverse engineer a desktop app",
         "hosting": "local", "how_to_invoke": "x"},
        {"domain": "skills", "name": "safe-git", "one_line": "commit safely in a shared worktree",
         "hosting": "local", "how_to_invoke": "x"},
    ]
    meta = {"built_at": t, "sources": {"gateway_tools": {"ok": True}}, "caller": CALLER}
    return {"cards": cards, "parts": {"user": dict(meta), "cwd": dict(meta)}}


OK_RECEIPT = {"exit_code": 0, "ts": 0.0}


def _ok():
    return dict(OK_RECEIPT, ts=time.time())


def test_query_reaches_tools_and_skills_and_scores_are_normalised() -> None:
    res = rank.query("segment objects in video masks", live=False, merged=_merged(),
                     current_fp=FP)
    doms = {c["domain"] for c in res["cards"]}
    assert {"tools", "skills"} <= doms
    assert all(0 < c["score"] <= 1.0 for c in res["cards"])


def test_live_hits_merge_but_cannot_bypass_the_index() -> None:
    res = rank.query("make a video", merged=_merged(), current_fp=FP,
                     search=lambda task, k, timeout: (["not_in_index", "sana_video"], ""))
    assert res["live"]["hits"] == 1
    assert [c["name"] for c in res["cards"] if c["domain"] == "tools"][0] == "sana_video"
    assert all(c["name"] != "not_in_index" for c in res["cards"])


def test_a_slow_gateway_falls_back_to_cached_cards() -> None:
    res = rank.query("segment objects in video masks", merged=_merged(), current_fp=FP,
                     search=lambda task, k, timeout: ([], "TimeoutError: timed out"))
    assert res["live"]["error"] and res["cards"]


def test_check_passes_fresh_and_fails_stale_empty_and_dead() -> None:
    assert check.check_index(merged=_merged(), refresh=_ok())[0] == 0
    assert check.check_index(merged=_merged(time.time() - 7 * 86400), refresh=_ok())[0] == 1
    assert check.check_index(merged={"cards": [], "parts": _merged()["parts"]},
                             refresh=_ok())[0] == 1
    dead = _merged()
    dead["parts"]["user"]["sources"]["gateway_tools"] = {"ok": False, "error": "down"}
    assert check.check_index(merged=dead, refresh=_ok())[0] == 1


def test_check_fails_when_nothing_refreshes_the_index() -> None:
    rc, lines = check.check_index(merged=_merged(), refresh=None)
    assert rc == 1 and any("no refresh receipt" in ln for ln in lines)
    old = {"exit_code": 0, "ts": time.time() - 3 * check.DEFAULT_MAX_AGE_S}
    assert check.check_index(merged=_merged(), refresh=old)[0] == 1
    failed = {"exit_code": 1, "ts": time.time(), "error": "boom"}
    assert check.check_index(merged=_merged(), refresh=failed)[0] == 1


def test_a_probe_that_shares_one_word_is_not_a_hit() -> None:
    nonsense = (("xyzzy plugh frobnicate video", ("tools", "skills"), ()),)
    rc, lines = check.check_index(merged=_merged(), refresh=_ok(), probes=nonsense)
    assert rc == 1 and any("covers" in ln for ln in lines)
    pinned = (("commit safely in a shared worktree", ("tools", "skills"),
               ("awgit", "safe-git")),)
    assert check.check_index(merged=_merged(), refresh=_ok(), probes=pinned)[0] == 0
    wrong = (("commit safely in a shared worktree", ("tools",), ("sana_video",)),)
    assert check.check_index(merged=_merged(), refresh=_ok(), probes=wrong)[0] == 1


def test_a_platform_index_answers_a_tenant_with_no_platform_cards() -> None:
    """Repro of the review: user.json built as the platform, queried by a tenant."""
    plat = dict(_merged())
    admin = {"domain": "tools", "name": "get_secret", "one_line": "read a secret from the vault",
             "hosting": "fleet", "source": "gateway", "how_to_invoke": "x",
             "fetched_for": "a1a1a1a1a1a1"}
    pcaller = {"tenant_id": "platform", "tier": "platform", "known": True,
               "fingerprint": "a1a1a1a1a1a1"}
    plat["cards"] = plat["cards"] + [admin]
    plat["parts"] = {p: dict(m, caller=pcaller) for p, m in plat["parts"].items()}
    as_owner = rank.query("read a secret from the vault", live=False, merged=plat,
                          current_fp="a1a1a1a1a1a1")
    assert any(c["name"] == "get_secret" for c in as_owner["cards"])
    as_tenant = rank.query("read a secret from the vault", live=False, merged=plat,
                           current_fp="b2b2b2b2b2b2")
    assert not any(c["source"] == "gateway" for c in as_tenant["cards"])
    assert as_tenant["caller"]["note"]


def test_ttl_reuse_never_crosses_callers(home, project) -> None:
    calls = []

    def src(names):
        def fn(_cwd):
            calls.append(names)
            return ([{"domain": "tools", "name": n, "hosting": "fleet", "source": "gateway",
                      "one_line": n} for n in names], {"ok": True, "count": len(names)})
        return [("gateway_tools", fn)]
    plat = scope.Caller(tenant_id="platform", tier="platform", known=True,
                        fingerprint="a1a1a1a1a1a1")
    tenant = scope.Caller(tenant_id="tnt_garg", tier="free", known=True,
                          fingerprint="b2b2b2b2b2b2")
    index.build_part("user", project, caller=plat,
                     table=src(["get_secret", "mint_node_token", "rag_query"]))
    # The worst case: the platform's file copied to where the tenant's part lives.
    index.part_path("user", project, fp=tenant.fingerprint).write_bytes(
        index.part_path("user", project, fp=plat.fingerprint).read_bytes())
    doc = index.build_part("user", project, caller=tenant, table=src(["rag_query"]))
    assert len(calls) == 2
    assert [c["name"] for c in doc["cards"]] == ["rag_query"]
    assert doc["sources"]["gateway_tools"]["reused"] is False
    assert index.part_path("user", project, fp=plat.fingerprint) != index.part_path(
        "user", project, fp=tenant.fingerprint)


def test_admin_tools_never_reach_a_free_tenant_even_from_its_own_list() -> None:
    tenant = scope.caller_from_me({"tenant_id": "tnt_garg", "tier": "free"},
                                  fingerprint="b2b2b2b2b2b2")
    for name in ("get_secret", "set_secret", "delete_secret", "list_secrets",
                 "mint_node_token", "provision_gateway_key", "provision_workspace",
                 "k3s_deploy", "ring_rollback", "hetzner_list_servers", "secretguard_purge",
                 "runners_delete_ghost", "restart_dgx_service", "dgx_start_containers",
                 "cloudflare_dns_add", "cf_dns_upsert", "fleet_control"):
        card = {"domain": "tools", "name": name, "hosting": "fleet", "source": "gateway",
                "fetched_for": tenant.fingerprint}
        assert scope.admit(card, tenant)[0] is False, name
    ok = {"domain": "tools", "name": "rag_query", "hosting": "fleet", "source": "gateway",
          "fetched_for": tenant.fingerprint}
    assert scope.admit(ok, tenant)[0] is True
    assert scope.admit(dict(ok, fetched_for="a1a1a1a1a1a1"), tenant)[0] is False
    assert scope.admit({k: v for k, v in ok.items() if k != "fetched_for"}, tenant)[0] is False


#: Real platform-only names from the gateway manifest (2026-10-21), used when this
#: machine has no gateway to ask.
MANIFEST_SAMPLE = ("get_secret", "set_secret", "delete_secret", "list_secrets",
                   "mint_node_token", "provision_gateway_key", "provision_workspace",
                   "k3s_deploy", "k3s_rollback", "ring_rollback", "ring_full_deploy",
                   "secretguard_purge", "restart_dgx_service", "runners_delete_ghost",
                   "rag_query", "recall")


def _manifest_cards(monkeypatch) -> list:
    """The live gateway manifest when a bearer and the gateway are here, else the sample."""
    monkeypatch.delenv("AITHER_HOME_OVERRIDE", raising=False)
    cards: list = []
    if sources._bearer():
        try:
            cards, _rec = sources.gateway_tools()
        except Exception:  # noqa: BLE001 - no gateway: judge the sample instead
            cards = []
    if not cards:
        cards = [{"domain": "tools", "name": n, "hosting": "fleet", "source": "gateway"}
                 for n in MANIFEST_SAMPLE]
    return cards


def test_the_live_manifest_admits_nothing_platform_only_for_a_free_tenant(monkeypatch) -> None:
    """The real gateway manifest, read as the platform built it: a free tenant is
    admitted NO gateway tool at all, and none of the admin families even from a list
    fetched for the tenant itself."""
    cards = _manifest_cards(monkeypatch)
    for c in cards:
        c["fetched_for"] = "a1a1a1a1a1a1"  # what the owner's build stamps
    tenant = scope.caller_from_me({"tenant_id": "tnt_garg", "tier": "free"},
                                  fingerprint="b2b2b2b2b2b2")
    kept, _ = scope.filter_cards(cards, tenant)
    assert kept == []
    for c in cards:
        c["fetched_for"] = tenant.fingerprint  # even a list fetched for the tenant
    kept, _ = scope.filter_cards(cards, tenant)
    families = ("get_secret", "set_secret", "k3s_", "ring_", "provision_", "mint_")
    assert not [c["name"] for c in kept if c["name"].startswith(families)]


def test_an_unknown_caller_tier_ranks_lowest() -> None:
    pro = scope.caller_from_me({"tenant_id": "tnt_garg", "tier": "pro"}, fingerprint="b" * 12)
    assert pro.tier == "community" and "pro" in pro.tier_error
    pack = {"domain": "packs", "name": "x", "hosting": "hosted", "min_tier": "enterprise"}
    assert scope.admit(pack, pro) == (False, "above caller tier")
    assert scope.admit(dict(pack, min_tier="no-such-tier"),
                       scope.caller_from_me({"tenant_id": "t", "tier": "enterprise"}))[0] is False


def test_a_lock_held_by_a_dead_pid_is_taken_over(tmp_path) -> None:
    lock = tmp_path / "x.lock"
    lock.write_text("999999999", encoding="utf-8")
    with index._Lock(lock) as held:
        assert held.held
    lock.write_text(str(os.getpid()), encoding="utf-8")
    with index._Lock(lock) as held:
        assert not held.held


def test_check_reads_file_mtime_so_a_touched_back_index_is_stale(home, project) -> None:
    caller = scope.Caller(tenant_id="t", tier="free", known=True)
    for part in ("user", "cwd"):
        index.build_part(part, project, caller=caller, table=_table(_merged()["cards"]))
    index.receipt_path().write_text(json.dumps(_ok()), encoding="utf-8")
    probes = (("reverse engineer a desktop app", ("skills",), ("rea",)),)
    assert check.check_index(str(project), probes=probes)[0] == 0
    week = time.time() - 7 * 86400
    os.utime(index.part_path("user", project), (week, week))
    rc, lines = check.check_index(str(project), probes=probes)
    assert rc == 1 and any(ln.startswith("CD001 user") for ln in lines)


def test_cli_self_test_passes_and_help_exits_zero(capsys) -> None:
    assert cli.main(["--self-test"]) == 0
    assert cli.main(["--help"]) == 0
    assert cli.main(["check", "--max-age", "soon"]) == 2


def test_parse_age() -> None:
    assert check.parse_age("6h") == 21600 and check.parse_age("90") == 90
