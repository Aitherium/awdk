"""Secure value capture for credential cards.

``kind="credential"`` cards never carry the value: the card names WHICH
secret is wanted (``secret_name``) and why (``credential_description``);
the owner enters the value through THIS module — a masked prompt — and it
goes straight to the vault, then the card closes with the
``CREDENTIAL_ANSWER`` marker.

The value's journey: a masked field (the terminal's ``getpass`` or the card
window's ``show="•"`` entry) -> ``vault_credential`` -> the store that owns
the card's ``credential_scope`` — the platform vault, the user lockbox, or
the workspace lockbox. It never touches the card store, the daemon's wire
format, the popup render, or a session transcript. This module never prints
the value.

What the card keeps afterwards is a RECEIPT, never the value: what was
stored, where, by which door, and a digest — signed with awseal when a key
is present. ``CREDENTIAL_ANSWER`` is one constant string, so without a
receipt every credential card that ever closed is indistinguishable from
every other, including one closed by somebody who stored nothing.

Why the masked prompt must be the ONLY door: before this module
existed, ``store.answer()`` accepted free text on an optionless credential
card — persisting the value in the card's durable JSON, which the daemon
serves over HTTP, the popup renders, and the steering mailbox copies into
session transcripts. The marker-only enforcement in ``store.answer()``
closes that; the prompt here is the designed entry point.

Doors, today: the card window's masked field ("Vault it here") and the
terminal's ``getpass`` both call ``vault_credential`` — that is the ONE door.
``launch_gui_prompt`` opens the host's standalone tkinter prompt bound to the
card (``secret_prompt.py --card``) as the fallback for a desktop where the
card window is not up; it closes the card itself with the same marker.
"""

from __future__ import annotations

import asyncio
import getpass
import hashlib
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

from adk.decisions.quiet import is_quiet
from adk.decisions.store import DecisionCard, DecisionError, DecisionStore

logger = logging.getLogger(__name__)

#: The host-side masked GUI, relative to the monorepo root. It is the SAME door
#: whether the raise came from the CLI or the card window: one command, one
#: window, and the window closes the card itself when the vault confirms the
#: write — so the raising session learns the outcome through the ordinary
#: answer mailbox and nobody has to invent a detector.
#:
#: Why nobody may poll instead: measured 2026-09-13, an agent launched this
#: prompt by hand next to a card and then waited for the vault value's LENGTH
#: to change. A replacement token has the same length as the one it replaces,
#: so three successful stores read as three failures and the owner was told
#: so three times. The prompt answering the card is the only honest signal.
GUI_PROMPT_REL = ("AitherOS", "scripts", "secret_prompt.py")

#: Windows only; on POSIX ``creationflags`` is invalid, so the dict is empty.
_GUI_NO_WINDOW = 0x08000000 | 0x00000008  # CREATE_NO_WINDOW | DETACHED_PROCESS


def find_repo_root(start: Optional[Path] = None,
                   marker: "tuple[str, ...]" = GUI_PROMPT_REL) -> Optional[Path]:
    """The checkout that carries ``marker`` (the GUI prompt by default), or None.

    ``AITHER_REPO_ROOT`` wins when set. Otherwise walk up from this module
    (it lives three or four levels under the root, depending on which package
    tree it was imported from) and then from the working directory. Returning
    None rather than guessing is deliberate: launching nothing and SAYING so
    beats launching a script that does not exist and reading the spawn's
    success as a window on screen.
    """
    candidates: list[Path] = []
    env = os.environ.get("AITHER_REPO_ROOT", "").strip()
    if env:
        candidates.append(Path(env))
    for origin in (start or Path(__file__).resolve(), Path.cwd()):
        candidates.extend([origin, *origin.parents])
    for base in candidates:
        if base.joinpath(*marker).is_file():
            return base
    return None


def has_display() -> bool:
    """Is there a desktop to put a window on? Windows always; POSIX needs DISPLAY."""
    if sys.platform == "win32":
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def gui_prompt_argv(card: DecisionCard, root: Path) -> list[str]:
    """The exact command that opens the masked GUI bound to ``card``.

    ``pythonw`` on Windows so the GUI owns no console; the script itself asks
    for the value, writes it to the vault, reads the LENGTH back and answers
    the card. Nothing here carries the value.
    """
    exe = Path(sys.executable)
    if sys.platform == "win32":
        windowed = exe.with_name("pythonw.exe")
        if windowed.is_file():
            exe = windowed
    argv = [str(exe), str(root.joinpath(*GUI_PROMPT_REL)), "--card", card.id]
    if card.secret_name:
        argv += ["--name", card.secret_name]
    scope = (card.credential_scope or "platform").strip().lower()
    argv += ["--scope", scope]
    if (card.credential_format or "").strip().lower() == "totp_seed":
        argv.append("--totp")
    return argv


def launch_gui_prompt(card: DecisionCard,
                      pid_out: "Optional[list[int]]" = None, *,
                      owner_initiated: bool = False) -> "tuple[bool, str]":
    """Open the masked GUI for a credential card, detached. Returns (launched, why).

    ``pid_out``, when given, receives the window's pid so a later ask can tell
    "the window is still up" from "the owner closed it -- open it again".

    Never raises: a card that cannot get a window is still a card, and the
    caller prints ``why`` so the owner knows to use ``awask answer <id>``
    (the terminal door) instead. ``why`` never contains a secret.

    ``owner_initiated`` is the owner pressing "open it" on a surface: that is
    not an auto-popup, so the quiet hold (a game in front, do-not-disturb) does
    not apply — the owner asked for the window, now. The test guard still does.
    """
    if (card.kind or "").strip().lower() != "credential":
        return False, f"{card.id} is not a credential ask — no prompt to open"
    # 🚩 A TEST RUN MUST NEVER PUT THIS DIALOG ON A HUMAN'S SCREEN. Measured
    # 2026-09-19: a pytest run of the credential-card suite spawned six "Store
    # credentials in AitherSecrets" windows over the owner's desktop, each
    # asking for a real key from a temp store that no longer existed. The
    # refusal is keyed on the env marker pytest sets for every test, so a NEW
    # test file is safe without remembering to stub anything; AITHER_NO_GUI_PROMPT
    # is the manual switch for a run that is not pytest. It is checked FIRST,
    # before quiet/display, so the refusal is deterministic rather than
    # host-dependent — and the test that pins this clears the marker to prove
    # the guard is the marker and not a dead path.
    if os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("AITHER_NO_GUI_PROMPT"):
        return False, ("masked prompt suppressed under test — answer at a terminal "
                       f"with: awask answer {card.id}")
    # Quiet (Do-not-disturb, or a full-screen game/app in front): the masked
    # dialog is topmost to win the raise, i.e. it lands over the game. Hold it.
    # The card is untouched -- still open, answerable from the terminal door.
    quiet, quiet_why = (False, "") if owner_initiated else is_quiet()
    if quiet:
        return False, (f"held while quiet: {quiet_why}; "
                       f"answer with adk decide answer {card.id}")
    if not has_display():
        return False, ("no display (DISPLAY unset) — masked prompt not launched; "
                       f"answer at a terminal with: awask answer {card.id}")
    root = find_repo_root()
    if root is None:
        return False, ("the masked GUI prompt is not in reach (set AITHER_REPO_ROOT "
                       "to the checkout that carries AitherOS/scripts/secret_prompt.py); "
                       f"answer at a terminal with: awask answer {card.id}")
    argv = gui_prompt_argv(card, root)
    # Detached and console-less: a detached parent has no console, so any
    # console-subsystem child would otherwise get a NEW window that takes focus.
    extra: dict = {}
    if os.name == "nt":
        extra["creationflags"] = _GUI_NO_WINDOW
    else:
        extra["start_new_session"] = True
    try:
        proc = subprocess.Popen(
            argv, cwd=str(root), stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **extra,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, (f"masked prompt failed to launch ({type(exc).__name__}: {exc}); "
                       f"answer at a terminal with: awask answer {card.id}")
    if pid_out is not None:
        pid_out.append(proc.pid)
    return True, (f"masked prompt launched for {card.secret_name or card.id} — "
                  f"it closes {card.id} itself once the vault confirms the write")


def _session_bearer() -> str:
    """The caller's Identity bearer — the same credential the MCP plane uses."""
    try:
        p = Path.home() / ".aither" / "session-bearer"
        if p.exists():
            return p.read_text().strip()
    except OSError as exc:
        # Falling back to the env credential is intentional — an unreadable
        # bearer file must not be a silent no-op, but must not hard-fail a
        # desktop where AITHER_API_KEY is the configured credential either.
        logger.debug("session-bearer unreadable (%s); falling back to env", exc)
    return os.environ.get("AITHER_API_KEY", "")


#: A command template that writes a secret, reading the VALUE on **stdin**.
#: ``{name}`` is substituted; the value is never an argv element (anything that
#: can read /proc would see it) and never an env var. Unset on an ordinary
#: machine, so this leg is simply skipped there.
#:
#: On a fleet host the vault is an IN-NETWORK name the host cannot resolve, so
#: the write has to happen inside a container that already holds the vault
#: credential — which also means the credential never materialises in a shell
#: variable on the host. Example:
#:   AITHER_SECRETS_EXEC=wsl -d awnix -u root podman exec -i
#:                       aitheros-security-core python3 /app/write.py {name}
SECRETS_EXEC_ENV = "AITHER_SECRETS_EXEC"


def _push_via_exec(key: str, value: str) -> tuple[bool, str]:
    """Leg 2 — pipe the value on stdin to a configured writer. Skipped if unset."""
    import shlex
    import subprocess

    template = os.environ.get(SECRETS_EXEC_ENV, "").strip()
    if not template:
        return False, "no " + SECRETS_EXEC_ENV
    # 🚨 posix=False on Windows, and it is load-bearing. shlex's POSIX mode
    # treats a backslash as an ESCAPE, so Windows paths with backslashes get
    # mangled (backslash + colon becomes just colon) — a path that does not exist. The
    # writer then exited 2 and the whole ladder reported "exited 2", which
    # reads as the vault refusing rather than as the command never being
    # spelled correctly. Measured on the first live run of this leg.
    argv = [part.replace("{name}", key)
            for part in shlex.split(template, posix=(os.name != "nt"))]
    # posix=False keeps surrounding quotes on a quoted argument; strip them so
    # a path with a space still resolves.
    argv = [a[1:-1] if len(a) > 1 and a[0] == a[-1] and a[0] in "\"'" else a
            for a in argv]
    # CREATE_NO_WINDOW: the card path runs DETACHED and a detached
    # process has NO console, so Windows allocates a NEW one for any
    # console-subsystem child — a window FLASHES and takes focus on every
    # single vault write. encoding= (explicit UTF-8) because `text=True` alone decodes
    # with the locale codec (cp1252 here), and a UnicodeDecodeError is a
    # ValueError, which the OSError/SubprocessError guard below does not
    # catch — the tool would crash rather than report the leg as failed.
    extra: dict = {}
    if os.name == "nt":
        extra["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    try:
        proc = subprocess.run(argv, input=value, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=120,
                              **extra)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, SECRETS_EXEC_ENV + " failed: " + type(exc).__name__
    if proc.returncode == 0:
        return True, "exec writer"
    # stderr can echo the payload on a badly written writer, so only the code
    # is reported — never the writer's output.
    return False, SECRETS_EXEC_ENV + " exited " + str(proc.returncode)


def _push_to_vault(key: str, value: str) -> tuple[bool, str]:
    """Write the value to a vault. Returns ``(ok, which_leg_or_why)``.

    🚨 This returned a bare ``False`` and logged "Failed to push secret to any
    vault", which is the least actionable sentence it could produce — measured
    live 2026-09-05, the owner typed a real secret into the card and got
    exactly that. THREE separate things were wrong and the message named none:
    the AitherSecrets leg was skipped entirely (``secrets_url=""`` was passed
    deliberately, and with ``AITHER_SECRETS_URL`` unset that branch cannot
    run), so it fell through to ``SecretsSync``'s default gateway —
    ``https://app.aitherium.com``, over the WAN — using a session bearer that
    was **ten days old**. Both legs failed, indistinguishably.

    So each leg now reports itself. A vault write is the one operation where
    "it did not work" without "here is which door was shut" costs the owner
    the whole retry.
    """
    tried: list[str] = []
    try:
        from adk.sync.secrets import SecretsSync
    except ImportError:  # awask without awdk: only the exec and fleet legs exist
        SecretsSync = None  # noqa: N806 - sentinel for "this leg cannot run"

    # Leg 1 — AitherSecrets direct (X-API-Key, service-to-service).
    direct = os.environ.get("AITHER_SECRETS_URL", "").strip()
    if direct and SecretsSync is None:
        tried.append("AitherSecrets(" + direct + ": adk not installed)")
    elif direct:
        api_key = _session_bearer()
        if api_key and asyncio.run(
                SecretsSync(api_key=api_key, secrets_url=direct).push(key, value)):
            return True, "AitherSecrets " + direct
        tried.append("AitherSecrets(" + direct + ")")
    else:
        tried.append("AitherSecrets(no AITHER_SECRETS_URL)")

    # Leg 2 — the stdin writer. The one that works on a fleet host.
    ok, why = _push_via_exec(key, value)
    if ok:
        return True, why
    tried.append(why)

    # Leg 3 — the remote gateway, last because it needs the WAN and a fresh
    # bearer, and its failure mode (a Cloudflare 403 challenge) reads as a
    # vault outage rather than as an expired credential.
    api_key = _session_bearer()
    if SecretsSync is None:
        tried.append("gateway(adk not installed)")
    elif api_key:
        if asyncio.run(SecretsSync(api_key=api_key, secrets_url="").push(key, value)):
            return True, "gateway"
        tried.append("gateway(rejected — bearer may be expired)")
    else:
        tried.append("gateway(no session bearer)")
    return False, "; ".join(tried)


#: The three scopes a credential card may name, and the three DIFFERENT stores
#: that own them. `credential_scope` was carried on the card, validated by the
#: CLI's `choices=`, rendered to the owner — and then ignored at the write, so
#: every value went to the PLATFORM vault whatever the card said. A `user`
#: secret landing in the platform vault is not a cosmetic routing bug: it is
#: readable by every platform admin and by services the owner never granted,
#: and nothing about it looks wrong afterwards. The card said one thing and the
#: write did another, which is the one failure this routing exists to prevent.
SCOPE_PLATFORM = "platform"
SCOPE_USER = "user"
SCOPE_WORKSPACE = "workspace"


def _genesis_base() -> str:
    """Where the lockbox planes live, or "" when nobody said.

    There is deliberately NO default. The old one was a plain-http loopback
    address, and on a fleet host that is wrong twice over: the lockbox router
    publishes no host port, so it refuses, and every fleet service speaks TLS, so
    plain http hangs or closes the socket. Either way the owner saw "lockbox
    unreachable" for a write the worker-container path would have landed. An
    unset base now skips this leg BY NAME, and the host path (leg 0) is the door.
    """
    return os.environ.get("AITHER_GENESIS_URL", "").strip().rstrip("/")


def _lockbox_write(scope: str, key: str, value: str) -> tuple[bool, str]:
    """Write to the lockbox that owns `scope`. Returns ``(ok, why)``.

    Reached over HTTP rather than by importing the monorepo's
    ``lib.lockbox``: adk ships to strangers, where that import would fail
    (the monorepo's lockbox module is not in the public package). The two
    endpoints are the ones the platform's own MCP tools call — ``PUT
    /lockbox/user/{name}`` and ``POST /workspace-secrets/set``.

    🚨 The response BODY is never put in the returned detail, even on an
    error. A rejecting endpoint can echo the request it rejected, and this
    function's caller prints the detail to a terminal and stores it on the
    card — so echoing a 4xx body here would put the secret in both of the
    places the whole module exists to keep it out of.
    """
    try:
        import httpx
    except ImportError:
        return False, scope + " lockbox (httpx not installed)"
    try:
        from adk._tls import tls_verify
    except ImportError:  # awask without awdk: verify with the system store
        def tls_verify():
            return True

    bearer = _session_bearer()
    if not bearer:
        return False, scope + " lockbox (no session bearer)"
    base = _genesis_base()
    if not base:
        return False, (scope + " lockbox (no AITHER_GENESIS_URL — the in-network "
                       "host path is the door for this scope)")
    if scope == SCOPE_USER:
        method, url = "PUT", base + "/lockbox/user/" + key
        payload: dict[str, Any] = {"value": value}
    else:
        method, url = "POST", base + "/workspace-secrets/set"
        payload = {"key": key, "value": value}
    headers = {"Authorization": "Bearer " + bearer,
               "Content-Type": "application/json"}
    try:
        with httpx.Client(timeout=15, verify=tls_verify()) as client:
            resp = client.request(method, url, json=payload, headers=headers)
    except (httpx.HTTPError, OSError) as exc:
        return False, (scope + " lockbox unreachable at " + base
                       + " (" + type(exc).__name__ + ")")
    if resp.status_code in (200, 201, 204):
        return True, scope + " lockbox at " + base
    return False, scope + " lockbox HTTP " + str(resp.status_code)


#: One-slot cache for the host vault module; `[]` unresolved, `[None]` absent.
_HOST_VAULT: list = []


def _host_vault_module():
    """The standalone GUI prompt's module, imported by path, or None off-fleet.

    Its ``vault_set``/``vault_len`` carry every measured trap of reaching the
    vault from a Windows host (the wsl ``$`` loss, CRLF on stdin, an empty
    curl status that is not an HTTP code). Importing it rather than copying it
    is the point: two copies of that list drift, and the half that drifts is
    the half that decides whether an owner's credential lands.
    """
    if _HOST_VAULT:
        return _HOST_VAULT[0]
    module = None
    root = find_repo_root()
    if root is not None:
        import importlib.util

        path = root.joinpath(*GUI_PROMPT_REL)
        spec = importlib.util.spec_from_file_location("aither_secret_prompt", path)
        if spec is not None and spec.loader is not None:
            try:
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
            except Exception as exc:  # noqa: BLE001 - an unloadable host script is "absent"
                logger.debug("host vault script unloadable (%s)", exc)
                module = None
    _HOST_VAULT.append(module)
    return module


def use_host_vault(module: Any) -> None:
    """Bind the host vault module explicitly — the host script binds ITSELF.

    The host script calls this before it hands a value to
    :func:`vault_credential`, so the write goes through the very module that is
    running (its stubs in a test, its engine cache in a session) rather than a
    second copy imported by path from whatever checkout happens to be nearest.
    """
    _HOST_VAULT[:] = [module]


def _push_via_fleet_host(key: str, value: str, scope: str) -> tuple[bool, str]:
    """Leg 0 — write through the fleet host's in-container path, then read back.

    The read-back must return the SAME length that was written. "Something is
    there" is not proof: a stale value of another length under the same name
    reads as present, and the card would close on a write that did not land.
    """
    host = _host_vault_module()
    if host is None:
        return False, "fleet host(no " + "/".join(GUI_PROMPT_REL) + " in reach)"
    try:
        ok, why = host.vault_set(key, value, scope=scope)
    except RuntimeError as exc:  # no engine / no vault-reaching container
        return False, "fleet host(" + str(exc) + ")"
    if not ok:
        return False, "fleet host(" + why + ")"
    try:
        n = host.vault_len(key, scope=scope)
    except RuntimeError as exc:
        return False, "fleet host(wrote, read-back failed: " + str(exc) + ")"
    if n <= 0:
        return False, "fleet host(wrote but did not read back)"
    # The user lockbox lists names only (no value read), so its read-back is
    # presence; the two scopes that can report a length must report THIS one.
    if scope != SCOPE_USER and n != len(value):
        return False, ("fleet host(wrote " + str(len(value)) + " chars, read back "
                       + str(n) + " — the write did not land)")
    return True, "fleet vault via the worker container (" + str(n) + " chars read back)"


def _store_for_scope(scope: str, key: str, value: str,
                     *, host_only: bool = False) -> tuple[bool, str]:
    """Route the value to the store that OWNS this scope — never a wider one.

    An unknown scope RAISES. It must not fall back to the platform vault: a
    fallback would take the one input we could not interpret and write it to
    the widest store on the platform, which is the worst possible response to
    not understanding the ask. Refusing leaves the card OPEN and names the
    scope, so the owner can retry rather than discover the secret elsewhere.
    """
    want = (scope or SCOPE_PLATFORM).strip().lower()
    if want not in (SCOPE_PLATFORM, SCOPE_USER, SCOPE_WORKSPACE):
        raise DecisionError(
            "credential scope " + repr(scope) + " has no storage door — refusing "
            "rather than defaulting to the platform vault. Known scopes: "
            + ", ".join((SCOPE_PLATFORM, SCOPE_USER, SCOPE_WORKSPACE)))
    # Leg 0 — a fleet host. The vault is an IN-NETWORK name this host cannot
    # resolve, so the write goes through the same engine-resolving,
    # in-container path the standalone GUI prompt uses (`<engine> exec -i
    # <worker container> sh -s`, value on stdin, never argv). One implementation of
    # "reach the vault from here", and the same read-back proof.
    ok, why = _push_via_fleet_host(key, value, want)
    if ok:
        return True, why
    if host_only:
        # The host script is the caller and IS the fleet path: a fallback leg
        # here would be a second writer with no read-back, which is the thing
        # "one door" exists to remove. Fail loudly; the card stays open.
        return False, why
    tried = [why]
    if want == SCOPE_PLATFORM:
        ok, why = _push_to_vault(key, value)
    else:
        ok, why = _lockbox_write(want, key, value)
    if ok:
        return True, why
    return False, "; ".join(tried + [why])


#: Bumped only when the signed payload's shape changes — a verifier meeting a
#: version it does not know must refuse rather than guess, the same contract
#: awseal's own SEAL_VERSION carries.
RECEIPT_VERSION = 1


def _sign_receipt(payload: dict[str, Any]) -> dict[str, Any]:
    """Sign the receipt with awseal, or record honestly that it is unsigned.

    A missing signing key must NOT block the vault write — the secret is
    already stored by the time this runs, and refusing here would leave a
    vaulted value with an OPEN card asking for it again. But an unsigned
    receipt must never be indistinguishable from a signed one, so `signed`
    is always present and a `reason` names why when it is False.

    awseal is imported guarded: adk installs on machines that have never
    heard of it, and a hard import would turn "no receipt" into "no
    credential cards work at all".
    """
    receipt: dict[str, Any] = {"version": RECEIPT_VERSION, "payload": payload,
                               "signed": False}
    try:
        from awseal import SealError, load_private_key, public_key_hex
        from awseal.seal import canonical
    except ImportError:
        receipt["reason"] = "awseal-not-installed"
        return receipt
    try:
        private = load_private_key()
        receipt["signature"] = private.sign(canonical(payload)).hex()
        receipt["public_key"] = public_key_hex(private)
        receipt["signed"] = True
    except (SealError, OSError) as exc:
        receipt["reason"] = "awseal-key-unavailable: " + type(exc).__name__
    return receipt


def resolve_card(card_id: str, store: DecisionStore) -> DecisionCard:
    """The credential card, or a DecisionError naming why it is not one."""
    card = store.get(card_id)
    if card is None:
        raise DecisionError(f"no such card: {card_id}")
    if (card.kind or "").strip().lower() != "credential":
        raise DecisionError(f"card {card_id} is not a credential ask")
    return card


def vault_credential(card_id: str, store: DecisionStore, value: str,
                     *, via: str = "cli-masked",
                     host_only: bool = False) -> tuple[int, str]:
    """Push an already-captured value to the vault and close the card.

    THE ONE DOOR. Both entry points — the terminal's masked ``getpass`` and
    the popup's masked field — call this, rather than each carrying its own
    copy of "push, then answer with the marker". Two copies of one rule drift,
    and the half that drifts here is the half that decides whether a secret
    reaches the card store: ``store.answer()`` accepts the CREDENTIAL_ANSWER
    marker and nothing else, so an entry point that forgot the push would
    close the ask having stored nothing, reporting success.

    Returns ``(code, detail)``: 0 vaulted and closed · 1 vault write FAILED
    (card left OPEN, so the ask is not lost) · 2 empty input refused (card
    left OPEN). ``detail`` names the leg that succeeded, or every leg that was
    tried and why each was shut. The VALUE is never returned, logged, printed
    or placed in ``detail``.
    """
    card = resolve_card(card_id, store)
    if not value:
        return 2, "nothing entered"
    label = card.secret_name or card.id
    scope = (card.credential_scope or SCOPE_PLATFORM).strip().lower()
    # THE ONLY frame that holds the value. The digest is taken here so the
    # receipt builder never receives it, which makes "nothing but this
    # function touches the secret" a property a reader can check by looking
    # at the call graph rather than a claim they have to trust.
    try:
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
        chars = len(value)
        ok, detail = _store_for_scope(scope, label, value, host_only=host_only)
    finally:
        del value  # never keep it longer than the write
    if not ok:
        return 1, detail
    receipt = _sign_receipt({
        "card_id": card.id,
        "secret_name": label,
        "scope": scope,
        "value_sha256": digest,
        "chars": chars,
        # The moment the WRITE returned. The store stamps its own
        # `answered_at` microseconds later, so the two are close and are NOT
        # byte-equal by design — a verifier must not require equality.
        "answered_at": time.time(),
        "via": via,
        "stored_via": detail,
    })
    # The note is the closed-loop signal the raising session reads — the same
    # "stored, N chars" the standalone GUI writes, never the value.
    store.answer(card_id, DecisionCard.CREDENTIAL_ANSWER, note=f"stored, {chars} chars",
                 via=via, receipt=receipt)
    return 0, detail + ("" if receipt.get("signed")
                        else " — receipt UNSIGNED (" + str(receipt.get("reason")) + ")")


def trusted_receipt_keys() -> set[str]:
    """Public keys whose receipts may close a card through the daemon.

    This host's own awseal key, plus any listed (comma-separated hex) in
    ``AITHER_CREDENTIAL_RECEIPT_KEYS``. The embedded ``public_key`` of a receipt
    is never trusted on its own: anyone can mint a key and sign anything with
    it, so a receipt that verifies only against itself proves nothing.
    """
    keys = {k.strip().lower() for k in
            os.environ.get("AITHER_CREDENTIAL_RECEIPT_KEYS", "").split(",") if k.strip()}
    try:
        from awseal import SealError, public_key_hex
    except ImportError:
        return keys
    try:
        keys.add(public_key_hex().lower())
    except (SealError, OSError) as exc:
        logger.debug("no local awseal key to trust (%s)", type(exc).__name__)
    return keys


def verify_receipt(receipt: Any, card: Optional[DecisionCard] = None,
                   *, trusted: Optional[set[str]] = None) -> tuple[bool, str]:
    """Is ``receipt`` a signed vault receipt for ``card``? ``(ok, why)``.

    Fails CLOSED on every shape it does not recognise: unsigned, an unknown
    version, a key nobody trusts, a signature that does not verify, or a
    payload naming another card or another secret.
    """
    if not isinstance(receipt, dict):
        return False, "no receipt"
    if receipt.get("version") != RECEIPT_VERSION:
        return False, "unknown receipt version " + repr(receipt.get("version"))
    if receipt.get("signed") is not True:
        return False, "receipt is unsigned (" + str(receipt.get("reason") or "?") + ")"
    payload = receipt.get("payload")
    if not isinstance(payload, dict):
        return False, "receipt has no payload"
    if card is not None:
        if payload.get("card_id") != card.id:
            return False, "receipt names another card"
        if card.secret_name and payload.get("secret_name") != card.secret_name:
            return False, "receipt names another secret"
    pub = str(receipt.get("public_key") or "").strip().lower()
    keys = trusted if trusted is not None else trusted_receipt_keys()
    if not pub or pub not in keys:
        return False, "receipt signed by an untrusted key"
    try:
        from awseal import load_public_key
        from awseal.seal import canonical
    except ImportError:
        return False, "awseal not installed — cannot verify"
    try:
        load_public_key(pub).verify(bytes.fromhex(str(receipt.get("signature") or "")),
                                    canonical(payload))
    except Exception as exc:  # noqa: BLE001 - any verify failure is a refusal
        return False, "signature does not verify (" + type(exc).__name__ + ")"
    return True, "signed by " + pub[:16]


def close_with_receipt(card_id: str, store: DecisionStore, receipt: Any,
                       *, via: str = "api") -> DecisionCard:
    """Close a credential card whose value was vaulted ELSEWHERE, on its receipt.

    The daemon's answer route lands here, so an off-box surface (the desk, Veil,
    MCP ``decisions_answer``) can close a card only by presenting the signed
    receipt the vault door produced. Raises DecisionError when it does not verify;
    the card stays open.
    """
    card = resolve_card(card_id, store)
    ok, why = verify_receipt(receipt, card)
    if not ok:
        raise DecisionError("credential card " + card_id + " stays open: " + why)
    chars = (receipt.get("payload") or {}).get("chars")
    note = f"stored, {chars} chars" if isinstance(chars, int) else "stored (receipt)"
    return store.answer(card_id, DecisionCard.CREDENTIAL_ANSWER, note=note, via=via,
                        deliver=False, receipt=receipt)


# ── reconcile: a secret already in the vault closes its own card ───────────────

#: A card that asks to REPLACE a value (rotate, re-issue, a refused/expired one)
#: is never closed on presence — the old value is exactly what is present. A
#: consumer whose stored value was refused (the mail poller's IMAP login) raises
#: its card with "refused" in the TITLE for exactly this reason. (Not a dedupe
#: key: the store re-keys every credential card to its secret family.)
#: 2026-10-14: "Proton Bridge password for owner@... (inbound mail stopped)" was closed as
#: "already in vault" -- the stored value was the broken one. An ask that names a FAILURE
#: (stopped, dead, failing, wrong, invalid, re-enter, no such user) is a replacement ask.
_REPLACE_ASK = re.compile(r"\b(rotat\w*|replac\w*|re-?issu\w*|refused|rejected|expired|"
                          r"revoked|stale|new (?:token|key|password)|stopped|dead|"
                          r"fail(?:ed|ing|s)?|wrong|invalid|re-?enter\w*|no such user)\b",
                          re.IGNORECASE)

#: ``presence(name, scope) -> int``: the stored LENGTH (>0 present, 0 absent,
#: <0 could not tell). Names and lengths only; a value never crosses it.
PresenceFn = Any


def _host_presence(name: str, scope: str) -> int:
    host = _host_vault_module()
    probe = getattr(host, "vault_present_len", None) if host is not None else None
    if probe is None:
        return -1
    try:
        return int(probe(name, scope=scope))
    except (RuntimeError, OSError, ValueError) as exc:
        logger.debug("presence probe for %s failed (%s)", name, type(exc).__name__)
        return -1


def _recipe_user(recipe: dict[str, Any], recipes_file: Optional[Path]) -> str:
    """``user_from: "<file>#<dotted.key>"`` resolved beside the recipes file."""
    spec = str(recipe.get("user_from") or "").strip()
    if not spec or "#" not in spec or recipes_file is None:
        return ""
    fname, _, dotted = spec.partition("#")
    target = recipes_file.parent / fname
    try:
        import yaml

        node: Any = yaml.safe_load(target.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - a missing user only narrows matching
        logger.debug("user_from %s unreadable (%s)", spec, type(exc).__name__)
        return ""
    for part in dotted.split("."):
        node = node.get(part) if isinstance(node, dict) else None
    return str(node or "").strip()


def _recipe_name_sets(card: DecisionCard, recipes: dict[str, dict[str, Any]],
                      recipes_file: Optional[Path]) -> list[list[str]]:
    """Every set of names whose joint presence satisfies ``card``.

    Always the card's own name; plus, for each recipe that claims it (canonical
    name, alias or superseded name), the recipe's canonical ``names``.
    """
    own = (card.secret_name or "").strip()
    sets: list[list[str]] = [[own]] if own else []
    for recipe in recipes.values():
        names = [str(n) for n in (recipe.get("names") or [])]
        if not names:
            continue
        claimed = {str(a) for a in (recipe.get("aliases") or [])}
        claimed |= {str(a) for a in (recipe.get("supersedes") or [])}
        users = []
        for tmpl in names:
            if "{user}" in tmpl and own:
                head, _, tail = tmpl.partition("{user}")
                if own.startswith(head) and own.endswith(tail) and len(own) > len(head + tail):
                    users.append(own[len(head):len(own) - len(tail)])
        cfg_user = _recipe_user(recipe, recipes_file)
        if cfg_user:
            users.append(cfg_user)
        if any("{user}" in n for n in names):
            # No user resolved = no concrete name: never probe a template.
            concrete = [[n.replace("{user}", u) for n in names]
                        for u in dict.fromkeys(u for u in users if u)]
        else:
            concrete = [names]
        if own in claimed or any(own in c for c in concrete):
            sets.extend(c for c in concrete if c not in sets)
    return sets


def reconcile_present(store: DecisionStore, *, presence: PresenceFn = None,
                      recipes: Optional[dict[str, dict[str, Any]]] = None,
                      recipes_file: Optional[Path] = None,
                      log: Any = None) -> list[tuple[str, str, int]]:
    """Close every open credential card whose secret is ALREADY in the vault.

    For each open credential card, ask ``presence`` (names and lengths only —
    never a value) about the card's own name and about every canonical name
    set of a recipe that claims it. The first fully-present set resolves the
    card with the note ``already in vault (len N)`` and a signed presence
    receipt. Returns ``[(card_id, name, length)]`` for the cards it closed.

    A presence answer of <0 ("could not tell") never closes anything: an
    unreachable vault must not read as a full one.
    """
    probe = presence or _host_presence
    say = log or (lambda msg: logger.info("%s", msg))
    if recipes is None:
        try:
            from adk.decisions.recipes import RecipeError, load_recipes, recipes_path

            recipes_file = recipes_file or recipes_path()
            recipes = load_recipes(recipes_file)
        except (ImportError, RecipeError) as exc:
            say("reconcile: no credential recipes (" + str(exc)[:120] + ") — own names only")
            recipes = {}
    closed: list[tuple[str, str, int]] = []
    for card in store.list(status="open"):
        if (card.kind or "").strip().lower() != "credential":
            continue
        if _REPLACE_ASK.search(card.title or ""):
            # TITLE only: summaries carry boilerplate ("a refused write leaves
            # the card open") that would match every card.
            # A rotation/replacement ask: the OLD value being present is the
            # premise of the card, not its answer. Leaving it open is today's
            # behaviour; closing it would silently drop a rotation.
            continue
        scope = (card.credential_scope or SCOPE_PLATFORM).strip().lower()
        for names in _recipe_name_sets(card, recipes, recipes_file):
            lengths = {n: probe(n, scope) for n in names}
            if not lengths or any(v <= 0 for v in lengths.values()):
                continue
            name = names[0]
            n = lengths[name]
            receipt = _sign_receipt({
                "card_id": card.id, "secret_name": card.secret_name or name,
                "scope": scope, "present": lengths, "via": "reconcile",
                "answered_at": time.time(),
            })
            try:
                store.resolve(card.id, note=f"already in vault (len {n})", receipt=receipt)
            except DecisionError as exc:
                say("reconcile: " + card.id + " not closed: " + str(exc)[:160])
                break
            say("reconcile: closed " + card.id + " — " + name + " already in vault "
                f"(len {n})")
            closed.append((card.id, name, n))
            break
    return closed


#: Seconds between reconcile passes in the daemon.
SWEEP_INTERVAL_S = 300


def start_reconcile_sweep(interval_s: Optional[float] = None) -> Any:
    """Run :func:`reconcile_present` every ``interval_s`` on a daemon thread.

    Returns the thread, or None when it is off: ``AITHER_CREDENTIAL_SWEEP=0``,
    under pytest, or with no host vault script in reach (nothing could answer a
    presence question, and a sweep that cannot see must not run as if it could).
    The first pass runs at once, so a restart closes satisfied cards within
    seconds rather than five minutes.
    """
    import threading

    if os.environ.get("AITHER_CREDENTIAL_SWEEP", "1").strip() in ("0", "false", "off"):
        return None
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return None
    if _host_vault_module() is None:
        logger.warning("credential sweep off: no %s in reach", "/".join(GUI_PROMPT_REL))
        return None
    every = float(interval_s or SWEEP_INTERVAL_S)
    stop = threading.Event()

    def _loop() -> None:
        from adk.decisions.store import get_store

        while not stop.is_set():
            try:
                reconcile_present(get_store())
            except Exception as exc:  # noqa: BLE001 - one bad pass must not end the sweep
                logger.warning("credential sweep pass failed: %s", type(exc).__name__)
            stop.wait(every)

    thread = threading.Thread(target=_loop, name="credential-sweep", daemon=True)
    thread.stop = stop  # type: ignore[attr-defined]
    thread.start()
    return thread


def capture_credential(card_id: str, store: DecisionStore) -> int:
    """Prompt (masked) at the TERMINAL for a credential card's value, vault it.

    The popup's field is the other entry point; both end in
    ``vault_credential`` so neither can diverge about where the value goes.
    """
    card = resolve_card(card_id, store)
    label = card.secret_name or card.id
    value = getpass.getpass(f"secret for {label} (masked): ")
    code, detail = vault_credential(card_id, store, value, via="cli-masked")
    if code == 0:
        print(f"{card_id}: {label} vaulted via {detail}")
    else:
        # The reason, at the terminal, is the whole difference between a retry
        # and a guess.
        print(f"{card_id}: NOT vaulted — {detail}", file=__import__("sys").stderr)
    return code
