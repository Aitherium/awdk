"""Process-wide egress guard for the awdk air-gap enforcer (AFRL G10).

Public surface; the implementation lives in ``adk.compliance._egress_guard``
so that running this module as ``__main__`` shares the one guard state that
``import adk`` installed. See that module for the choke points and limits.

CLI::

    python -m adk.compliance.egress_guard --status [--json]
    python -m adk.compliance.egress_guard --probe URL   # 1 blocked, 0 allowed, 2 error
    python -m adk.compliance.egress_guard --probe       # 0 sealed, 1 egress possible, 2 error
    python -m adk.compliance.egress_guard --self-test
"""

from __future__ import annotations

import sys

from adk.compliance._egress_guard import (  # noqa: F401 - re-exported surface
    _INSTALLED,
    _ORIGINALS,
    SEAL_PROBE_ADDR,
    EgressBlocked,
    autoinstall,
    egress_guard_status,
    install,
    install_egress_guard,
    install_if_enforced,
    main,
    status,
    uninstall,
    uninstall_egress_guard,
    uvicorn_loop,
)

__all__ = [
    "SEAL_PROBE_ADDR",
    "EgressBlocked",
    "autoinstall",
    "egress_guard_status",
    "install",
    "install_egress_guard",
    "install_if_enforced",
    "main",
    "status",
    "uninstall",
    "uninstall_egress_guard",
    "uvicorn_loop",
]

if __name__ == "__main__":
    sys.exit(main())
