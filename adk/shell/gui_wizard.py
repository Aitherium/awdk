"""Aither for Home — first-run GUI wizard.

A big-button, plain-language onboarding wizard for a non-technical user. It
drives the SAME engine the CLI uses (`adk.shell.onboarding` helpers +
`adk.shell.image_setup`), but renders every step as a friendly page instead of
a terminal prompt. The user never sees a shell, a config file, or a flag.

Pages:
    0. Welcome — "Let's set up your AI assistant"
    1. Account  — create an account / log in (API key made automatically)
    2. Hardware — "Your computer can:" plain-language capability
    3. Image studio — offered only when the hardware can run it
    4. Bonsai   — "it also works in your web browser, nothing to install"
    5. Done     — next steps + API-key caution

Engineering rules:
  * Tkinter is imported LAZILY so `import adk.shell.gui_wizard` never fails on
    a headless box or in a test that has no display. `run()` handles that.
  * All engine work (auth, image setup) runs in a worker thread and is posted
    back to the UI with `after()` — Tkinter is not thread-safe, and the UI
    must never freeze during a multi-second detect or a model download.
  * `run(mode="auto", ...)` is the headless entry for tests / the standalone
    binary's `--yes` path: it executes the same steps and returns a dict.

Reuse (do not reinvent):
  * onboarding._register_account / _get_api_key / _save_auth / _detect_gpu
  * adk.shell.image_setup.image_studio_status / image_studio_run
"""

from __future__ import annotations

import asyncio
import json
import os
import queue
import sys
import threading
import webbrowser
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

# Loggers only — no tkinter at module scope.
import logging

logger = logging.getLogger("adk.gui_wizard")

_PORTAL_URL = "https://api.aitherium.com"
_ART_URL = "https://aitherium.com"


# ── Headless engine (shared by the GUI pages and run(mode="auto")) ────────


def _auth_helpers():
    """Import the onboarding engine helpers lazily (keeps import light)."""
    from adk.shell import onboarding as _ob

    return _ob


class _State:
    """Mutable result accumulator shared between the GUI and the engine."""

    def __init__(self):
        self.email: str = ""
        self.api_key: str = ""
        self.tenant_id: str = ""
        self.endpoint: str = ""
        self.gpu_detected: str = ""
        self.hardware: Dict[str, Any] = {}
        self.brain_choice: str = "skip"
        self.image: Dict[str, Any] = {}
        self.image_choice: str = "skip"
        self.steps: List[str] = []
        self.errors: List[str] = []

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": "success" if not self.errors else "partial",
            "email": self.email,
            "api_key": self.api_key,
            "tenant_id": self.tenant_id,
            "endpoint": self.endpoint,
            "gpu_detected": self.gpu_detected,
            "hardware": self.hardware,
            "image": self.image,
            "steps_completed": self.steps,
            "errors": self.errors,
        }


def _create_account(state: _State, email: str, password: str) -> bool:
    """Register (or log in) and mint an API key. Returns success."""
    ob = _auth_helpers()
    try:
        auth_result = asyncio.run(ob._register_account(email, password))
        token = auth_result.get("token", "")
        user_id = auth_result.get("user_id", "")
        key_result = asyncio.run(ob._get_api_key(token))
        api_key = key_result.get("api_key", key_result.get("key", ""))
        if not api_key:
            state.errors.append("Account created but no API key was returned.")
            return False
        ob._save_auth(email, token, api_key, user_id)
        state.email = email
        state.api_key = api_key
        state.tenant_id = user_id
        state.endpoint = ob.GATEWAY_URL
        state.steps.append("account_ready")
        return True
    except Exception as e:  # noqa: BLE001 — surface to the user, never crash
        logger.exception("account setup failed")
        state.errors.append(f"Could not create your account: {e}")
        return False


def _detect_hardware(state: _State) -> None:
    """Fill state.hardware with a plain-language capability summary."""
    from adk.shell.image_setup import image_studio_status

    state.hardware = image_studio_status()
    if state.hardware.get("gpu_name"):
        state.gpu_detected = state.hardware["gpu_name"]
    state.steps.append("hardware_detected")


def _run_image_studio(
    state: _State, apply: bool, recipe_id: str = "", prefer_engine: str = "auto"
) -> None:
    """Run image_studio_run in the current thread; store the result."""
    from adk.shell.image_setup import image_studio_run

    state.image = image_studio_run(
        recipe_id=recipe_id or state.hardware.get("recommended_recipe", ""),
        prefer_engine=prefer_engine,
        dry_run=not apply,
        auto_apply=apply,
    )
    if state.image.get("status") in ("healthy", "planned", "deferred"):
        state.steps.append("image_studio_ready")
    elif state.image.get("status") == "failed":
        state.errors.append(state.image.get("plain_english", "Image studio setup failed."))


# ── Tkinter app ───────────────────────────────────────────────────────────


def _require_tk():
    import tkinter as tk  # noqa: PLC0415 — lazy import
    from tkinter import ttk  # noqa: PLC0415
    return tk, ttk


class _WizardApp:
    """Multi-page Tkinter wizard. Constructed only when a display exists."""

    PAGES = ("welcome", "account", "hardware", "brain", "image", "bonsai", "done")

    def __init__(self, state: _State, auto_image: bool = False):
        tk, ttk = _require_tk()
        self.tk = tk
        self.ttk = ttk
        self.state = state
        self.auto_image = auto_image
        self.page_index = 0

        self.root = tk.Tk()
        self.root.title("Aither — Set up your AI assistant")
        self.root.geometry("760x560")
        self.root.minsize(680, 500)
        try:
            self.root.configure(bg="#0f172a")
        except Exception:  # noqa: BLE001 — theming is cosmetic
            pass

        self.container = tk.Frame(self.root, bg="#0f172a")
        self.container.pack(fill="both", expand=True)
        self._build_pages()
        self._show_page(0)

        # Worker threads post results here; _poller drains it on the main loop.
        # (Tkinter is not thread-safe — never call root.after from a thread.)
        self._results: "queue.Queue" = queue.Queue()
        self.root.after(80, self._poller)

    def _post(self, fn: Callable, *args: Any) -> None:
        """Schedule a callback to run on the Tk main loop from any thread."""
        self._results.put((fn, args))

    def _poller(self) -> None:
        """Drain the worker-result queue on the main loop."""
        try:
            while True:
                fn, args = self._results.get_nowait()
                try:
                    fn(*args)
                except Exception as e:  # noqa: BLE001 — a bad callback must not kill the UI
                    logger.exception("wizard callback failed: %s", e)
        except queue.Empty:
            pass
        self.root.after(80, self._poller)

    # ── Page scaffolding ──────────────────────────────────────────────────

    def _frame(self, pad: int = 28) -> Any:
        return self.tk.Frame(self.container, bg="#0f172a")

    def _h1(self, parent: Any, text: str) -> None:
        self.tk.Label(
            parent, text=text, font=("Segoe UI", 22, "bold"),
            bg="#0f172a", fg="#f1f5f9", wraplength=660, justify="left",
        ).pack(anchor="w", pady=(0, 6))

    def _h2(self, parent: Any, text: str) -> None:
        self.tk.Label(
            parent, text=text, font=("Segoe UI", 14),
            bg="#0f172a", fg="#94a3b8", wraplength=660, justify="left",
        ).pack(anchor="w", pady=(0, 16))

    def _body(self, parent: Any, text: str) -> None:
        self.tk.Label(
            parent, text=text, font=("Segoe UI", 12),
            bg="#0f172a", fg="#cbd5e1", wraplength=660, justify="left",
        ).pack(anchor="w", pady=(0, 10))

    def _button(self, parent: Any, text: str, command: Callable,
                big: bool = False, kind: str = "primary") -> None:
        bg = "#06b6d4" if kind == "primary" else "#1e293b"
        fg = "#062a2e" if kind == "primary" else "#e2e8f0"
        self.tk.Button(
            parent, text=text, command=command,
            font=("Segoe UI", 14 if big else 12, "bold" if big else "normal"),
            bg=bg, fg=fg, activebackground="#0e7490" if kind == "primary" else "#334155",
            activeforeground="#062a2e" if kind == "primary" else "#e2e8f0",
            padx=26, pady=10 if big else 8, relief="flat", cursor="hand2",
            highlightthickness=0, bd=0,
        ).pack(anchor="w", pady=(12, 0))

    def _text(self, parent: Any, show: str = "") -> Any:
        e = self.tk.Entry(
            parent, font=("Segoe UI", 13), show=show or None,
            bg="#1e293b", fg="#f1f5f9", insertbackground="#f1f5f9",
            relief="flat", width=36,
        )
        return e

    def _status(self, parent: Any, text: str) -> Any:
        lbl = self.tk.Label(
            parent, text=text, font=("Segoe UI", 11),
            bg="#0f172a", fg="#fbbf24", wraplength=660, justify="left",
        )
        lbl.pack(anchor="w", pady=(14, 0))
        return lbl

    # ── Page builders ─────────────────────────────────────────────────────

    def _build_pages(self) -> None:
        self.frames: Dict[str, Any] = {}
        for name in self.PAGES:
            self.frames[name] = self._frame()
        self._build_welcome()
        self._build_account()
        self._build_hardware()
        self._build_brain()
        self._build_image()
        self._build_bonsai()
        self._build_done()

    def _build_welcome(self) -> None:
        f = self.frames["welcome"]
        self._h1(f, "Welcome to Aither 🎉")
        self._h2(f, "Let's set up your own AI assistant — in a few simple steps.")
        self._body(
            f,
            "This sets up an assistant that lives on YOUR computer and can:\n"
            "• Chat with you and help you get things done\n"
            "• Make images with Stable Diffusion using your own graphics card\n"
            "• Connect to aitherium.com so your apps are all in one place\n\n"
            "We'll ask a few simple questions. No technical knowledge needed.",
        )
        self._button(f, "Let's go →", lambda: self._go(1), big=True)

    def _build_account(self) -> None:
        f = self.frames["account"]
        self._h1(f, "Step 1 of 4 — Your account")
        self._h2(f, "One account lets your computer talk to aitherium.com.")
        self._body(f, "Use an email you check regularly. We'll send you a secure link.")
        self.email_entry = self._text(f)
        self.email_entry.pack(anchor="w", pady=(0, 10))
        self._body(f, "Choose a password (at least 8 characters).")
        self.pass_entry = self._text(f, show="•")
        self.pass_entry.pack(anchor="w", pady=(0, 10))
        self.acct_status = self._status(f, "")
        self._button(f, "Create my account →", self._do_account, big=True)

    def _do_account(self) -> None:
        email = self.email_entry.get().strip()
        password = self.pass_entry.get()
        if not email or "@" not in email or len(password) < 8:
            self.acct_status.config(
                text="Please enter your email and a password of at least 8 characters.",
                fg="#f87171",
            )
            return
        self.acct_status.config(text="Working… this can take a moment.", fg="#fbbf24")
        self._disable_page_buttons()
        threading.Thread(target=self._account_worker, args=(email, password), daemon=True).start()

    def _account_worker(self, email: str, password: str) -> None:
        ok = _create_account(self.state, email, password)
        self._post(self._account_done, ok)

    def _account_done(self, ok: bool) -> None:
        if ok:
            self.acct_status.config(text="✓ Account ready.", fg="#4ade80")
            self._button(self.frames["account"], "Next: check your computer →",
                         lambda: self._go(2))
        else:
            self.acct_status.config(
                text=self.state.errors[-1] if self.state.errors else "Something went wrong.",
                fg="#f87171",
            )
            self._button(self.frames["account"], "Try again", self._do_account)

    def _build_hardware(self) -> None:
        f = self.frames["hardware"]
        self._h1(f, "Step 2 of 4 — Your computer")
        self._h2(f, "We'll take a quick look at what your computer can do.")
        self.hw_status = self._status(f, "")
        self._button(f, "Check my computer →", self._do_hardware, big=True)

    def _do_hardware(self) -> None:
        self.hw_status.config(text="Looking at your computer…", fg="#fbbf24")
        self._disable_page_buttons()
        threading.Thread(target=self._hardware_worker, daemon=True).start()

    def _hardware_worker(self) -> None:
        _detect_hardware(self.state)
        self._post(self._hardware_done)

    def _hardware_done(self) -> None:
        hw = self.state.hardware
        f = self.frames["hardware"]
        self.hw_status.config(text="✓ Done.", fg="#4ade80")
        plain = hw.get("plain_english", "Your computer is ready.")
        self._body(f, plain)
        if hw.get("gpu_name"):
            self._body(f, f"Graphics: {hw['gpu_name']}")
        image_capable = bool(hw.get("capable")) and not hw.get("errors")
        self._image_capable = image_capable
        if not getattr(self, "_image_options_filled", False):
            self._fill_image_options()
            self._image_options_filled = True
        self._button(f, "Next →", lambda: self._go(3))

    # ── Local brain (your own model, optional) ──────────────────────────────

    BRAIN_MODELS = [
        # (key, label, plain, argv) — "python" is a sentinel for adk.cli.
        ("orchestrator", "Orchestrator (recommended)",
         "A small fast model for everyday chatting, ~5GB.",
         ["python", "setup", "llamacpp", "--non-interactive"]),
        ("gemma4-12b", "gemma4-12b",
         "A mid-size model, good quality, ~8GB.",
         ["ollama", "pull", "gemma4:12b"]),
        ("qwen3.6-27b", "qwen3.6-27b",
         "A larger, smarter model, ~16GB.",
         ["ollama", "pull", "qwen3.6:27b"]),
        ("deepseek-r1-14b", "deepseek-r1-14b",
         "Great at step-by-step reasoning, ~9GB.",
         ["ollama", "pull", "deepseek-r1:14b"]),
    ]

    def _build_brain(self) -> None:
        f = self.frames["brain"]
        self._h1(f, "Step 3 — Your AI's brain")
        self._h2(f, "Aither runs on YOUR computer. Pick the model that fits your machine:")
        self.brain_status = self._status(f, "")
        for key, label, plain, _args in self.BRAIN_MODELS:
            self._button(f, f"{label} — {plain}", lambda k=key: self._brain_pick(k))
        self._button(f, "Not now (start with cloud)", lambda: self._go(4), kind="secondary")

    def _brain_pick(self, key: str) -> None:
        self.state.brain_choice = key
        entry = next((m for m in self.BRAIN_MODELS if m[0] == key), None)
        if not entry:
            self._go(4)
            return
        self.brain_status.config(
            text=f"Installing {entry[1]}… this downloads a few GB and can take a while.",
            fg="#fbbf24",
        )
        self._disable_page_buttons()
        threading.Thread(target=self._brain_worker, args=(key, entry[3]), daemon=True).start()

    def _brain_worker(self, key: str, args: list) -> None:
        import subprocess as _sp

        if args and args[0] == "python":
            cmd = [sys.executable, "-m", "adk.cli", *args[1:]]
        else:
            cmd = list(args)
        try:
            proc = _sp.run(cmd, capture_output=True, text=True, timeout=7200)
            ok = proc.returncode == 0
        except Exception as e:  # noqa: BLE001
            ok = False
            logger.warning("local brain install failed: %s", e)
        self.state.steps.append(f"brain_{key}" if ok else f"brain_{key}_failed")
        if not ok:
            self.state.errors.append(f"The {key} model could not be installed.")
        self._post(self._brain_done, key, ok)

    def _brain_done(self, key: str, ok: bool) -> None:
        self.brain_status.config(
            text="✓ " + (f"Your {key} brain is ready." if ok else
                         "We'll keep going — you can install a model later."),
            fg="#4ade80" if ok else "#f87171",
        )
        self._button(self.frames["brain"], "Next →", lambda: self._go(4))

    def _build_image(self) -> None:
        f = self.frames["image"]
        self._h1(f, "Step 4 — Image studio")
        self._h2(f, "Make images right on your computer. Pick how you'd like to make them:")
        self.img_status = self._status(f, "")
        self.img_choice_buttons: List[Any] = []
        # Buttons are filled in _hardware_done once engine_options are known.
        self._button(f, "Not now", lambda: self._go(5), kind="secondary")

    def _fill_image_options(self) -> None:
        """Render one button per image engine this hardware can run."""
        f = self.frames["image"]
        opts = self.state.hardware.get("engine_options", [])
        if not opts:
            # Nothing runnable locally — the browser option is always the fallback.
            opts = [{
                "id": "bonsai-browser",
                "name": "In your web browser",
                "plain": "Make images right on aitherium.com — no download needed.",
                "recipe_id": "",
                "requires_download_gb": 0.0,
            }]
        for opt in opts:
            label = opt.get("name", opt.get("id", "?")).replace(" (Sana)", "")
            self._button(
                f,
                f"• {label}",
                lambda o=opt: self._image_pick(o),
            )

    def _image_pick(self, opt: Dict[str, Any]) -> None:
        oid = opt.get("id", "")
        recipe = opt.get("recipe_id", "")
        if oid == "bonsai-browser" or not recipe:
            # Zero-install path: nothing to run; carry a friendly note.
            self.state.image = {
                "status": "deferred",
                "plain_english": "You chose in-browser Bonsai — nothing to install.",
                "notes": ["On aitherium.com, Bonsai runs right in your browser."],
            }
            self.state.steps.append("image_studio_browser")
            self._go(5)
            return
        self.state.image_choice = oid
        self.img_status.config(
            text=f"Setting up {opt.get('name', oid)}… large files may take a while.",
            fg="#fbbf24",
        )
        self._disable_page_buttons()
        threading.Thread(
            target=self._image_worker, args=(recipe, oid), daemon=True
        ).start()

    def _image_worker(self, recipe: str, engine: str) -> None:
        _run_image_studio(self.state, apply=True, recipe_id=recipe, prefer_engine=engine)
        self._post(self._image_done)

    def _image_done(self) -> None:
        img = self.state.image
        f = self.frames["image"]
        if img.get("status") in ("healthy", "applied", "planned", "deferred"):
            self.img_status.config(text="✓ " + img.get("plain_english", "Ready."), fg="#4ade80")
        else:
            self.img_status.config(text=img.get("plain_english", "Could not set up the studio."), fg="#f87171")
        for note in img.get("notes", []) or []:
            self._body(f, note)
        self._button(f, "Next →", lambda: self._go(5))

    def _build_bonsai(self) -> None:
        f = self.frames["bonsai"]
        self._h1(f, "Step 5 — Your AI in the browser")
        self._h2(f, "Aither also works right in your web browser — nothing to install.")
        self._body(
            f,
            "Bonsai runs in your browser and works on most modern computers.\n"
            "When you visit aitherium.com, your assistant is right there.",
        )
        self._button(f, "Continue →", lambda: self._go(6), big=True)

    def _build_done(self) -> None:
        f = self.frames["done"]
        self._h1(f, "You're all set! 🎉")
        self._h2(f, "Here's what you can do next:")
        self._body(
            f,
            "1. Start chatting with your assistant (it's on your computer).\n"
            "2. Visit aitherium.com and sign in — your apps are waiting.\n"
            "3. If you set up the image studio, try making an image with Stable Diffusion.\n\n"
            "Your account is created and your computer is connected.",
        )
        self._button(f, "Open aitherium.com", lambda: webbrowser.open(_PORTAL_URL), big=True)
        self._button(f, "Finish", self._close, kind="secondary")

    # ── Navigation ────────────────────────────────────────────────────────

    def _go(self, index: int) -> None:
        self.page_index = index
        self._show_page(index)

    def _show_page(self, index: int) -> None:
        for name in self.PAGES:
            self.frames[name].pack_forget()
        self.frames[self.PAGES[index]].pack(fill="both", expand=True)

    def _disable_page_buttons(self) -> None:
        # Disable every button in the current page so the user cannot double-fire.
        page = self.frames[self.PAGES[self.page_index]]
        for child in page.winfo_children():
            try:
                child.configure(state="disabled")
            except Exception:  # noqa: BLE001 — only ttk/tk widgets have state
                pass

    def _close(self) -> None:
        self.root.destroy()

    def run(self) -> _State:
        self.root.mainloop()
        return self.state


# ── Public entry points ───────────────────────────────────────────────────


def run(
    *,
    mode: str = "interactive",
    email: str = "",
    password: str = "",
    auto_image: bool = False,
) -> Dict[str, Any]:
    """Run the wizard.

    ``mode="interactive"`` launches the Tkinter app when a display is
    available; if Tkinter is missing or Tk fails to initialise, it falls back
    to the headless engine path so the caller always gets a result dict.

    ``mode="auto"`` (or ``email``/``password`` supplied) runs the engine
    steps headlessly — used by tests, CI, and the standalone binary's
    ``--yes`` path.
    """
    state = _State()
    auto = mode == "auto" or bool(email and password)

    if auto:
        if email and password:
            _create_account(state, email, password)
        _detect_hardware(state)
        if auto_image:
            _run_image_studio(state, apply=True)
        return state.to_dict()

    # Interactive: try Tk, fall back to the engine path on failure.
    try:
        _require_tk()
        app = _WizardApp(state, auto_image=auto_image)
        final = app.run()
        return final.to_dict()
    except Exception as e:  # noqa: BLE001 — headless box, CI, or no display
        logger.info("GUI unavailable (%s) — running headless engine", e)
        _detect_hardware(state)
        if auto_image:
            _run_image_studio(state, apply=True)
        return state.to_dict()


# ── CLI entry (also the standalone binary's `wizard --gui` handler) ───────


def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    p = argparse.ArgumentParser(prog="aither wizard --gui")
    p.add_argument("--auto", action="store_true", help="headless (no window)")
    p.add_argument("--auto-image", action="store_true", help="also run image setup headless")
    p.add_argument("--email", default="", help="account email (auto mode)")
    p.add_argument("--password", default="", help="account password (auto mode)")
    p.add_argument("--json", action="store_true", help="print result as JSON")
    args = p.parse_args(argv)

    result = run(
        mode="auto" if args.auto else "interactive",
        email=args.email,
        password=args.password,
        auto_image=args.auto_image,
    )
    if args.json:
        print(json.dumps(result, indent=2, default=str))
    return 0 if result.get("status") != "failed" else 1


if __name__ == "__main__":
    sys.exit(main())
