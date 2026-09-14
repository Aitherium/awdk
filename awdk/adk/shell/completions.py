"""
Shell completions for AitherShell.

Supports bash, zsh, fish, PowerShell via argcomplete + static scripts.

Setup:
    # bash (add to ~/.bashrc)
    eval "$(register-python-argcomplete aither)"

    # zsh (add to ~/.zshrc)
    eval "$(register-python-argcomplete aither)"

    # fish
    register-python-argcomplete --shell fish aither | source

    # PowerShell (add to $PROFILE)
    Register-ArgumentCompleter -Native -CommandName aither -ScriptBlock {
        param($wordToComplete, $commandAst, $cursorPosition)
        $env:_ARGCOMPLETE = 1
        $env:_ARGCOMPLETE_IFS = "`n"
        $env:COMP_LINE = $commandAst.ToString()
        $env:COMP_POINT = $cursorPosition
        aither 2>&1 | ForEach-Object { $_ }
    }
"""

import os
from adk._tls import tls_verify
import sys


def setup_argcomplete(parser):
    """Enable argcomplete on the parser if available."""
    try:
        import argcomplete

        # Custom completers for dynamic values
        def model_completer(prefix, **kwargs):
            """Complete model names from Genesis."""
            try:
                import httpx
                url = os.environ.get("AITHER_URL", "https://localhost:8001")
                r = httpx.get(f"{url}/models", timeout=2.0, verify=tls_verify())
                if r.status_code == 200:
                    data = r.json()
                    models = []
                    for tier_models in data.get("tiers", {}).values():
                        models.extend(tier_models)
                    return [m for m in models if m.startswith(prefix)]
            except Exception:
                pass
            return ["aither-orchestrator", "deepseek-r1:14b", "llama3.2:3b"]

        def will_completer(prefix, **kwargs):
            """Complete will names."""
            try:
                import httpx
                url = os.environ.get("AITHER_WILL_URL", "https://localhost:8097")
                r = httpx.get(f"{url}/wills", timeout=2.0, verify=tls_verify())
                if r.status_code == 200:
                    data = r.json()
                    wills = data.get("wills", data if isinstance(data, list) else [])
                    return [w["id"] for w in wills if w["id"].startswith(prefix)]
            except Exception:
                pass
            return ["default", "aither-prime", "private-mode", "creative-mode"]

        def agent_completer(prefix, **kwargs):
            """Complete agent names."""
            agents = [
                "atlas", "demiurge", "lyra", "vera", "hera", "hydra",
                "ignis", "terra", "aeros", "saga", "iris", "prometheus",
                "aither", "mrrobot", "wrath", "lust",
            ]
            return [a for a in agents if a.startswith(prefix)]

        # Attach completers to specific arguments
        for action in parser._actions:
            if hasattr(action, "dest"):
                if action.dest == "model":
                    action.completer = model_completer
                elif action.dest == "will":
                    action.completer = will_completer
                elif action.dest in ("delegate", "persona"):
                    action.completer = agent_completer

        argcomplete.autocomplete(parser)
    except ImportError:
        pass  # argcomplete not installed — completions disabled


def print_completion_script(shell: str = "bash"):
    """Print shell completion script to stdout."""
    if shell in ("bash", "zsh"):
        print('eval "$(register-python-argcomplete aither)"')
    elif shell == "fish":
        print("register-python-argcomplete --shell fish aither | source")
    elif shell in ("powershell", "pwsh"):
        print("""Register-ArgumentCompleter -Native -CommandName aither -ScriptBlock {
    param($wordToComplete, $commandAst, $cursorPosition)
    $env:_ARGCOMPLETE = 1
    $env:_ARGCOMPLETE_IFS = "`n"
    $env:COMP_LINE = $commandAst.ToString()
    $env:COMP_POINT = $cursorPosition
    aither 2>&1 | ForEach-Object { $_ }
}""")
    else:
        print(f"Unknown shell: {shell}. Supported: bash, zsh, fish, powershell")
        sys.exit(1)
