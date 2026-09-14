"""AitherShell harness layer — one shell that drives every coding shell.

Public surface:

    from adk.harnesses import SessionManager, SessionConfig, detect

    detect()                              # what this box can drive
    mgr = SessionManager()
    s = mgr.create(SessionConfig(harness="claude", model_profile="deepseek-flash"))
    mgr.send(s.id, "refactor foo.py")
    s.events_since(0)                     # normalized event stream
"""

from __future__ import annotations

from adk.harnesses.events import EventKind, HarnessEvent
from adk.harnesses.manager import ManagerError, SessionManager, default_manager
from adk.harnesses.models import ModelBinding, ProfileError, list_profiles, resolve_binding
from adk.harnesses.registry import SPECS, HarnessSpec, LaunchSpec, Transport, detect, get
from adk.harnesses.session import HarnessSession, SessionConfig, SessionState

__all__ = [
    "EventKind",
    "HarnessEvent",
    "HarnessSession",
    "HarnessSpec",
    "LaunchSpec",
    "ManagerError",
    "ModelBinding",
    "ProfileError",
    "SPECS",
    "SessionConfig",
    "SessionManager",
    "SessionState",
    "Transport",
    "default_manager",
    "detect",
    "get",
    "list_profiles",
    "resolve_binding",
]
