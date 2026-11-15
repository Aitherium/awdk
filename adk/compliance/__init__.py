"""AitherShell compliance module — air-gap enforcement, attestation, model licenses.

Self-contained compliance for regulated industries (law, finance, healthcare).
No AitherOS service dependencies — file-based audit logging only.
"""

from importlib import import_module as _import_module

# Names resolve on first access (PEP 562), not at import: ``import adk`` loads
# ``adk.compliance._egress_guard`` (which needs only ``air_gap``) on every cold
# start, and the attestation / model-license modules would otherwise ride along
# inside the 150 ms budget the stranger-install gate holds ``import adk`` to.
_LAZY = {
    "AirGapEnforcer": "air_gap",
    "AirGapViolation": "air_gap",
    "EnforcementMode": "air_gap",
    "enforce_outbound_request": "air_gap",
    "get_air_gap_enforcer": "air_gap",
    "is_air_gap_enforced": "air_gap",
    "generate_attestation_report": "attestation",
    "AttestationReport": "attestation",
    "EgressBlocked": "_egress_guard",
    "egress_guard_status": "_egress_guard",
    "install_egress_guard": "_egress_guard",
    "uninstall_egress_guard": "_egress_guard",
    "ModelLicenseRegistry": "model_licenses",
    "get_model_license_registry": "model_licenses",
}


def __getattr__(name):
    mod = _LAZY.get(name)
    if mod is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(_import_module(f"{__name__}.{mod}"), name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(_LAZY))


__all__ = [
    "AirGapEnforcer",
    "AirGapViolation",
    "AttestationReport",
    "EgressBlocked",
    "EnforcementMode",
    "ModelLicenseRegistry",
    "egress_guard_status",
    "enforce_outbound_request",
    "generate_attestation_report",
    "get_air_gap_enforcer",
    "get_model_license_registry",
    "install_egress_guard",
    "is_air_gap_enforced",
    "uninstall_egress_guard",
]
