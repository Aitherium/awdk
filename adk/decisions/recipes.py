"""Credential recipes: the provider-side steps as DATA, and the post-landing proof.

A recipe (``config/credential_recipes.yaml``) names the provider, the vault key,
the dashboard, the ordered steps and the permission rows an owner needs to mint a
credential — so an agent renders them onto the card instead of typing them from
memory. It also carries a ``verify`` probe: after the value lands in the vault,
``awask credential-verify <card-id>`` runs that probe INSIDE the fleet (the
vault's hostname only resolves there), with the value read into memory in the
container and never printed, and records only the named ``record`` fields and
each scope probe's pass/fail as card facts.

Why (measured 2026-09-13): the Cloudflare rotation card carried a permission table
typed from memory, and the scope check happened by hand, forty minutes late.
Two halves of one ask that a recipe makes mechanical.

PyYAML is an OPTIONAL dependency here: the card store and window stay stdlib.
Without it the recipe commands say so and exit 2 — they never guess.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional

from adk.decisions.secure_prompt import find_repo_root
from adk.decisions.store import DecisionCard, DecisionError, DecisionStore

RECIPES_REL = ("AitherOS", "config", "credential_recipes.yaml")
RECIPES_ENV = "AITHER_CREDENTIAL_RECIPES"
#: The host-side vault script; its engine/container resolution is the ONE
#: implementation of "reach the vault from this host", with every measured trap
#: (wsl `$` loss, CRLF on stdin, empty output ≠ HTTP status) already in it.
HOST_PROMPT_REL = ("AitherOS", "scripts", "secret_prompt.py")


class RecipeError(Exception):
    """A recipe problem the caller should print and exit 2 on."""


# ── loading ────────────────────────────────────────────────────────────────────


def recipes_path() -> Optional[Path]:
    """The YAML: ``AITHER_CREDENTIAL_RECIPES`` if set, else the checkout's copy."""
    env = os.environ.get(RECIPES_ENV, "").strip()
    if env:
        return Path(env)
    root = find_repo_root(marker=RECIPES_REL)
    return root.joinpath(*RECIPES_REL) if root else None


def load_recipes(path: Optional[Path] = None) -> dict[str, dict[str, Any]]:
    """``{id: recipe}``. Raises RecipeError when the file or PyYAML is absent."""
    target = path or recipes_path()
    if target is None or not target.is_file():
        raise RecipeError(
            "no credential recipes file: set " + RECIPES_ENV + " or run from the "
            "checkout that carries " + "/".join(RECIPES_REL))
    try:
        import yaml
    except ImportError as exc:
        raise RecipeError("credential recipes need PyYAML (pip install pyyaml): "
                          + str(exc)) from exc
    try:
        raw = yaml.safe_load(target.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise RecipeError(f"cannot parse {target}: {exc}") from exc
    recipes = (raw or {}).get("recipes") if isinstance(raw, dict) else None
    if not isinstance(recipes, dict) or not recipes:
        raise RecipeError(f"{target} has no `recipes:` mapping")
    return {str(k): (v or {}) for k, v in recipes.items()}


def get_recipe(recipe_id: str, path: Optional[Path] = None) -> dict[str, Any]:
    recipes = load_recipes(path)
    want = (recipe_id or "").strip()
    if want not in recipes:
        raise RecipeError(f"unknown recipe {want!r}; known: " + ", ".join(sorted(recipes)))
    return recipes[want]


# ── rendering onto the card ───────────────────────────────────────────────────


def render_summary(recipe: dict[str, Any]) -> str:
    parts = [str(recipe.get("why") or "").strip()]
    if recipe.get("dashboard"):
        parts.append(f"Dashboard: {recipe['dashboard']}")
    return "\n".join(p for p in parts if p)


def render_permissions(recipe: dict[str, Any]) -> list[str]:
    """One row per line, ``Group · Item · Level`` — readable on a phone."""
    rows = []
    for row in recipe.get("permissions") or []:
        if isinstance(row, (list, tuple)):
            rows.append(" · ".join(str(cell) for cell in row))
        elif isinstance(row, dict):
            rows.append(" · ".join(str(v) for v in row.values()))
        else:
            rows.append(str(row))
    return rows


def render_detail(recipe: dict[str, Any]) -> str:
    lines: list[str] = []
    steps = [str(s) for s in (recipe.get("steps") or [])]
    if steps:
        lines.append(f"STEPS ({recipe.get('provider', 'provider')}):")
        lines += [f"  {i}. {step}" for i, step in enumerate(steps, start=1)]
    perms = render_permissions(recipe)
    if perms:
        lines.append("")
        lines.append("PERMISSIONS (Group · Item · Level):")
        lines += [f"  {row}" for row in perms]
    rotation = recipe.get("rotation") or {}
    if isinstance(rotation, dict) and rotation:
        lines.append("")
        lines.append("AFTERWARDS:")
        lines += [f"  {k}: {v}" for k, v in rotation.items()]
    return "\n".join(lines)


# ── the in-fleet verify probe ─────────────────────────────────────────────────

#: Runs INSIDE the vault-reaching container as ``python3 -``. The spec (URLs,
#: expectations, record paths — never a value) is embedded as a literal. The
#: credential is fetched from the vault into a local variable, used as a bearer,
#: and the only thing written to stdout is the JSON verdict on the last line.
_PROBE_TEMPLATE = r'''
import json, ssl, sys, urllib.error, urllib.request
SPEC = json.loads(%(spec)s)
VAULT = SPEC["vault_url"]
KEY = __import__("os").environ.get("AITHER_INTERNAL_SECRET", "")
out = {"ok": True, "could_not_run": "", "status": None, "record": {}, "probes": [],
       "failures": []}

def vault(name):
    req = urllib.request.Request(VAULT + "/secrets/" + name, headers={"X-API-Key": KEY})
    ctx = None
    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
                return json.loads(resp.read().decode("utf-8")).get("value") or ""
        except urllib.error.HTTPError as exc:
            # Named, because a 404 here is "no such secret" and a 404 from the
            # provider is a different fault; the two must not read alike.
            raise RuntimeError("vault GET /secrets/" + name + " -> HTTP " + str(exc.code))
        except (ssl.SSLError, urllib.error.URLError) as exc:
            if attempt == 2 or not isinstance(getattr(exc, "reason", exc), ssl.SSLError):
                raise RuntimeError("vault unreachable for " + name + ": " + str(exc))
            # The container trusts no internal CA for this hostname: same hop
            # the host script makes with `curl -sk`, in-network only, never
            # for the provider call.
            ctx = ssl._create_unverified_context()
    return ""

def call(method, url, body, token):
    headers = {"Authorization": "Bearer " + token, "User-Agent": "awask-credential-verify"}
    data = None
    if body is not None:
        data = body.encode("utf-8") if isinstance(body, str) else json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            status, raw = resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read()
    try:
        parsed = json.loads(raw.decode("utf-8")) if raw else {}
    except ValueError:
        parsed = {}
    return status, parsed

def dig(obj, path):
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur

def check(name, status, body, spec):
    problems = []
    want_status = spec.get("expect_status")
    if want_status is not None and int(status) != int(want_status):
        problems.append("%%s: HTTP %%s, expected %%s" %% (name, status, want_status))
    for path, want in (spec.get("expect") or {}).items():
        got = dig(body, path)
        if want == "*":
            if got in (None, "", [], {}):
                problems.append("%%s: %%s is empty" %% (name, path))
        elif got != want:
            problems.append("%%s: %%s=%%r, expected %%r" %% (name, path, got, want))
    bad = spec.get("fail_if_error_code")
    if bad is not None:
        codes = [e.get("code") for e in (body.get("errors") or []) if isinstance(e, dict)]
        if int(bad) in [c for c in codes if isinstance(c, int)]:
            problems.append("%%s: provider error code %%s (token lacks this scope)" %% (name, bad))
    return problems

try:
    token = vault(SPEC["vault_name"])
    if not token:
        out["could_not_run"] = "vault has no value for " + SPEC["vault_name"]
    else:
        fills = {}
        for name in SPEC.get("placeholders", []):
            fills[name] = vault(name)
            if not fills[name]:
                out["could_not_run"] = "vault has no value for placeholder " + name
        def fill(text):
            for k, v in fills.items():
                text = text.replace("{" + k + "}", v)
            return text
        if not out["could_not_run"]:
            main = SPEC["verify"]
            status, body = call(main.get("method", "GET"), fill(main["url"]),
                                main.get("body"), token)
            out["status"] = status
            for path in main.get("record") or []:
                out["record"][path] = dig(body, path)
            out["failures"] += check("verify", status, body, main)
            for probe in main.get("scope_probes") or []:
                pstatus, pbody = call(probe.get("method", "GET"), fill(probe["url"]),
                                      probe.get("body"), token)
                problems = check(probe.get("name", "probe"), pstatus, pbody, probe)
                out["probes"].append({"name": probe.get("name", "probe"),
                                      "status": pstatus, "ok": not problems,
                                      "detail": "; ".join(problems)})
                out["failures"] += problems
            del token
except Exception as exc:
    out["could_not_run"] = type(exc).__name__ + ": " + str(exc)
out["ok"] = not out["failures"] and not out["could_not_run"]
sys.stdout.write("\nAWASK-VERIFY " + json.dumps(out) + "\n")
'''


def _placeholders(verify: dict[str, Any]) -> list[str]:
    """Every ``{NAME}`` in a probe URL or body — each is a vault key."""
    import re

    found: list[str] = []
    texts = [str(verify.get("url") or ""), str(verify.get("body") or "")]
    for probe in verify.get("scope_probes") or []:
        texts += [str(probe.get("url") or ""), str(probe.get("body") or "")]
    for text in texts:
        for name in re.findall(r"\{([A-Z][A-Z0-9_]+)\}", text):
            if name not in found:
                found.append(name)
    return found


def _host_prompt_module():
    """The host vault script, imported by path — its ``_sh_in_container`` is the door."""
    root = find_repo_root(marker=HOST_PROMPT_REL)
    if root is None:
        raise RecipeError("credential-verify runs from a fleet host: "
                          + "/".join(HOST_PROMPT_REL) + " not found (set AITHER_REPO_ROOT)")
    path = root.joinpath(*HOST_PROMPT_REL)
    spec = importlib.util.spec_from_file_location("aither_secret_prompt", path)
    if spec is None or spec.loader is None:
        raise RecipeError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_verify(recipe: dict[str, Any], *, runner=None) -> dict[str, Any]:
    """Execute the recipe's probe in the fleet. Returns the verdict dict.

    ``runner(script, timeout) -> (rc, stdout)`` is injectable for the self-test;
    the default is the host script's ``_sh_in_container``. Raises RecipeError
    when the probe could not RUN (that is exit 2, never a pass).
    """
    verify = recipe.get("verify") or {}
    method = str(verify.get("method") or "none").strip().lower()
    if method == "none":
        return {"ok": True, "no_probe": True, "record": {}, "probes": [], "failures": []}
    if str(verify.get("auth") or "bearer").strip().lower() != "bearer":
        raise RecipeError(f"verify.auth {verify.get('auth')!r} is not supported (bearer only)")
    if runner is None:
        host = _host_prompt_module()
        vault_url = getattr(host, "VAULT_URL", "https://aitheros-secrets:8111")

        def runner(script: str, timeout: int) -> "tuple[int, str]":
            return host._sh_in_container(script, timeout)  # noqa: SLF001 - the one door
    else:
        vault_url = "https://aitheros-secrets:8111"
    spec = {
        "vault_url": vault_url,
        "vault_name": recipe.get("vault_name"),
        "placeholders": _placeholders(verify),
        "verify": {**verify, "method": method.upper()},
    }
    body = _PROBE_TEMPLATE % {"spec": json.dumps(json.dumps(spec))}
    # `python3 -` reads the program from stdin; a heredoc keeps it byte-exact
    # across the Windows -> WSL boundary (a `$` in argv would not survive).
    script = "python3 - <<'AWASK_PROBE'\n" + body + "\nAWASK_PROBE\n"
    rc, out = runner(script, 120)
    marker = "AWASK-VERIFY "
    line = next((ln[len(marker):] for ln in reversed(out.splitlines())
                 if ln.startswith(marker)), "")
    if not line:
        raise RecipeError(f"the in-fleet probe produced no verdict (exec rc={rc}) — "
                          "curl/python never ran; is the vault-reaching container up?")
    try:
        verdict = json.loads(line)
    except ValueError as exc:
        raise RecipeError(f"unreadable probe verdict: {exc}") from exc
    if verdict.get("could_not_run"):
        raise RecipeError("probe could not run: " + str(verdict["could_not_run"]))
    return verdict


def verdict_facts(recipe_id: str, verdict: dict[str, Any]) -> list[str]:
    """What goes on the card: record fields and probe verdicts, never a value."""
    stamp = time.strftime("%Y-%m-%d %H:%M")
    facts = []
    if verdict.get("no_probe"):
        return [f"verify {recipe_id} {stamp}: no probe for this recipe"]
    head = f"verify {recipe_id} {stamp}: " + ("PASS" if verdict.get("ok") else "FAIL")
    if verdict.get("status") is not None:
        head += f" (HTTP {verdict['status']})"
    facts.append(head)
    for path, value in (verdict.get("record") or {}).items():
        facts.append(f"  {path} = {value}")
    for probe in verdict.get("probes") or []:
        facts.append(f"  scope {probe.get('name')}: "
                     + ("pass" if probe.get("ok") else "FAIL — " + str(probe.get("detail"))))
    for failure in verdict.get("failures") or []:
        if not any(failure in f for f in facts):
            facts.append(f"  {failure}")
    return facts


def verify_card(card_id: str, store: DecisionStore, *, runner=None,
                recipes_file: Optional[Path] = None) -> "tuple[int, list[str]]":
    """``awask credential-verify``: 0 all pass · 1 a scope/value failed · 2 could not run."""
    card = store.get(card_id)
    if card is None:
        raise DecisionError(f"no such card: {card_id}")
    if (card.kind or "").strip().lower() != "credential":
        raise DecisionError(f"card {card_id} is not a credential ask")
    recipe_id = (card.credential_recipe or "").strip()
    if not recipe_id:
        raise DecisionError(f"card {card_id} carries no recipe — raise it with "
                            "`awask ask --credential --recipe <id>` to make it verifiable")
    recipe = get_recipe(recipe_id, recipes_file)
    verdict = run_verify(recipe, runner=runner)
    facts = verdict_facts(recipe_id, verdict)
    store.add_facts(card_id, facts)
    if verdict.get("no_probe"):
        return 0, facts
    return (0 if verdict.get("ok") else 1), facts


# ── self-test ─────────────────────────────────────────────────────────────────


def _self_test() -> int:
    """Prove the recipe plane can still fail, without a fleet."""
    import tempfile

    failures: list[str] = []

    def check(name: str, condition: bool, detail: str = "") -> None:
        print(f"  {'ok  ' if condition else 'FAIL'} {name} {detail if not condition else ''}")
        if not condition:
            failures.append(name)

    with tempfile.TemporaryDirectory() as tmp:
        yaml_path = Path(tmp) / "recipes.yaml"
        yaml_path.write_text(
            "version: 1\nrecipes:\n"
            "  demo:\n    provider: Demo\n    vault_name: DEMO_TOKEN\n    format: api_key\n"
            "    title: 'Demo token'\n    why: because\n    dashboard: https://x.example/\n"
            "    steps: ['one', 'two']\n    permissions: [[Zone, DNS, Edit]]\n"
            "    verify:\n      method: GET\n      url: https://x.example/{DEMO_ZONE}/v\n"
            "      auth: bearer\n      expect: {ok: true}\n      record: [id]\n"
            "      scope_probes:\n"
            "        - {name: p1, url: https://x.example/p, expect_status: 200}\n"
            "  silent:\n    provider: S\n    vault_name: S_TOKEN\n    format: api_key\n"
            "    title: s\n    why: w\n    steps: ['a']\n    verify: {method: none, record: []}\n",
            encoding="utf-8")
        try:
            recipes = load_recipes(yaml_path)
        except RecipeError as exc:
            print(f"  SKIP recipe plane untested: {exc}")
            return 0
        check("loads recipes", set(recipes) == {"demo", "silent"})
        try:
            get_recipe("nope", yaml_path)
            check("unknown id is refused", False, "accepted")
        except RecipeError as exc:
            check("unknown id is refused and lists the ids", "demo" in str(exc), str(exc))
        demo = recipes["demo"]
        detail = render_detail(demo)
        check("detail numbers the steps", "1. one" in detail and "2. two" in detail)
        check("detail renders a permission row", "Zone · DNS · Edit" in detail)
        check("summary carries why + dashboard",
              "because" in render_summary(demo) and "https://x.example/" in render_summary(demo))
        check("placeholders come from URLs", _placeholders(demo["verify"]) == ["DEMO_ZONE"])

        os.environ["AITHER_DECISIONS_DIR"] = str(Path(tmp) / "cards")
        os.environ["AITHER_STEER_DIR"] = str(Path(tmp) / "steer")
        store = DecisionStore(Path(tmp) / "cards")
        card = store.create(DecisionCard.from_dict({
            "id": "", "title": "t", "kind": "credential", "secret_name": "DEMO_TOKEN",
            "credential_format": "api_key", "credential_scope": "platform",
            "credential_description": "d", "credential_recipe": "demo"}))
        check("recipe id survives the store round-trip",
              (store.get(card.id) or card).credential_recipe == "demo")

        def fake_runner(verdict: dict):
            def run(script: str, timeout: int):
                # The script must carry the spec and the marker, and never a value.
                assert "AWASK-VERIFY" in script and "DEMO_TOKEN" in script
                return 0, "noise\nAWASK-VERIFY " + json.dumps(verdict) + "\n"
            return run

        passing = {"ok": True, "could_not_run": "", "status": 200, "record": {"id": "abc"},
                   "probes": [{"name": "p1", "status": 200, "ok": True, "detail": ""}],
                   "failures": []}
        code, facts = verify_card(card.id, store, runner=fake_runner(passing),
                                  recipes_file=yaml_path)
        check("a passing probe exits 0", code == 0, str(code))
        check("record fields land as facts", any("id = abc" in f for f in facts), str(facts))
        check("facts are written to the card",
              any("scope p1: pass" in f for f in (store.get(card.id) or card).facts))

        failing = {**passing, "ok": False,
                   "probes": [{"name": "p1", "status": 403, "ok": False,
                               "detail": "p1: HTTP 403, expected 200"}],
                   "failures": ["p1: HTTP 403, expected 200"]}
        code, facts = verify_card(card.id, store, runner=fake_runner(failing),
                                  recipes_file=yaml_path)
        check("a failed scope exits 1", code == 1, str(code))
        check("the failure is named on the card", any("403" in f for f in facts))

        try:
            verify_card(card.id, store, runner=lambda s, t: (1, ""), recipes_file=yaml_path)
            check("no verdict is could-not-run, not a pass", False, "returned")
        except RecipeError:
            check("no verdict is could-not-run, not a pass", True)
        try:
            verify_card(card.id, store, recipes_file=yaml_path,
                        runner=fake_runner({**passing, "could_not_run": "vault empty"}))
            check("an in-fleet could_not_run propagates as exit 2", False, "returned")
        except RecipeError:
            check("an in-fleet could_not_run propagates as exit 2", True)

        silent = store.create(DecisionCard.from_dict({
            "id": "", "title": "t", "kind": "credential", "secret_name": "S_TOKEN",
            "credential_format": "api_key", "credential_scope": "platform",
            "credential_description": "d", "credential_recipe": "silent"}))
        code, facts = verify_card(silent.id, store, runner=lambda s, t: (0, ""),
                                  recipes_file=yaml_path)
        check("method: none exits 0 and says so", code == 0 and "no probe" in facts[0])

        plain = store.create(DecisionCard.from_dict({
            "id": "", "title": "t", "kind": "credential", "secret_name": "X",
            "credential_format": "api_key", "credential_scope": "platform",
            "credential_description": "d"}))
        try:
            verify_card(plain.id, store, runner=lambda s, t: (0, ""), recipes_file=yaml_path)
            check("a card without a recipe is refused", False, "accepted")
        except DecisionError:
            check("a card without a recipe is refused", True)

    print()
    if failures:
        print(f"SELF-TEST FAILED — {len(failures)} check(s): {', '.join(failures)}")
        return 1
    print("self-test passed — recipes render, verdicts land as facts, and failures fail")
    return 0


if __name__ == "__main__":
    sys.exit(_self_test())
