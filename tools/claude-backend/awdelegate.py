"""Compatibility launcher for the local research delegate.

The implementation lives in awdk (`awdk/adk/awdelegate.py`) so that every machine
which installs awdk gets it. Existing user-scope MCP registrations still point at
this path, so this file loads that module directly instead of importing the `adk`
package: under `python -S` there is no site-packages, and `import adk` would pull
third-party dependencies the delegate does not use.

It also re-exports the module's names, so `import awdelegate` from a sibling
(bench_stack.py calls awdelegate.research) keeps working, not only the CLI.

    python -S awdelegate.py --mcp
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load():
    target = Path(__file__).resolve().parents[2] / "awdk" / "adk" / "awdelegate.py"
    if target.is_file():
        spec = importlib.util.spec_from_file_location("_awdk_awdelegate", target)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    try:
        import adk.awdelegate as module
    except ImportError as exc:
        raise SystemExit(f"awdelegate: awdk is not installed and {target} is missing ({exc})")
    return module


_impl = _load()
globals().update({k: v for k, v in vars(_impl).items() if not k.startswith("__")})

if __name__ == "__main__":
    sys.exit(_impl.main())
