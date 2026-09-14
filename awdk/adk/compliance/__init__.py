"""AitherShell compliance module — air-gap enforcement, attestation, model licenses.

Self-contained compliance for regulated industries (law, finance, healthcare).
No AitherOS service dependencies — file-based audit logging only.
"""

from adk.compliance.air_gap import (
    AirGapEnforcer,
    AirGapViolation,
    EnforcementMode,
    get_air_gap_enforcer,
    is_air_gap_enforced,
)
from adk.compliance.attestation import generate_attestation_report, AttestationReport
from adk.compliance.model_licenses import (
    ModelLicenseRegistry,
    get_model_license_registry,
)

__all__ = [
    "AirGapEnforcer",
    "AirGapViolation",
    "AttestationReport",
    "EnforcementMode",
    "ModelLicenseRegistry",
    "generate_attestation_report",
    "get_air_gap_enforcer",
    "get_model_license_registry",
    "is_air_gap_enforced",
]
