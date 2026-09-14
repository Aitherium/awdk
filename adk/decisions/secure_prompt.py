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
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

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


def launch_gui_prompt(card: DecisionCard) -> "tuple[bool, str]":
    """Open the masked GUI for a credential card, detached. Returns (launched, why).

    Never raises: a card that cannot get a window is still a card, and the
    caller prints ``why`` so the owner knows to use ``awask answer <id>``
    (the terminal door) instead. ``why`` never contains a secret.
    """
    if (card.kind or "").strip().lower() != "credential":
        return False, f"{card.id} is not a credential ask — no prompt to open"
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
        subprocess.Popen(
            argv, cwd=str(root), stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **extra,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, (f"masked prompt failed to launch ({type(exc).__name__}: {exc}); "
                       f"answer at a terminal with: awask answer {card.id}")
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
#:   AITHER_SECRETS_EXEC=wsl -d Debian -u root podman exec -i
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
    """Where the lockbox planes live. Same env var the rest of adk uses."""
    return os.environ.get(
        "AITHER_GENESIS_URL", "http://localhost:8001").strip().rstrip("/")


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


def _push_via_fleet_host(key: str, value: str, scope: str) -> tuple[bool, str]:
    """Leg 0 — write through the fleet host's in-container path, then read back."""
    host = _host_vault_module()
    if host is None:
        return False, "fleet host(no " + "/".join(GUI_PROMPT_REL) + " in reach)"
    try:
        ok, why = host.vault_set(key, value, scope=scope)
    except RuntimeError as exc:  # no engine / no vault-reaching container
        return False, "fleet host(" + str(exc) + ")"
    if not ok:
        return False, "fleet host(" + why + ")"
    n = host.vault_len(key, scope=scope)
    if n <= 0:
        return False, "fleet host(wrote but did not read back)"
    return True, "fleet vault via the worker container (" + str(n) + " chars read back)"


def _store_for_scope(scope: str, key: str, value: str) -> tuple[bool, str]:
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
    # aither-worker sh -s`, value on stdin, never argv). One implementation of
    # "reach the vault from here", and the same read-back proof.
    ok, why = _push_via_fleet_host(key, value, want)
    if ok:
        return True, why
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
                     *, via: str = "cli-masked") -> tuple[int, str]:
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
        ok, detail = _store_for_scope(scope, label, value)
    finally:
        del value  # never keep it longer than the write
    if not ok:
        return 1, detail
    receipt = _sign_receipt({
        "card_id": card.id,
        "secret_name": label,
        "scope": scope,
        "value_sha256": digest,
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
    return 0, detail


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
