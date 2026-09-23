"""The local-root placeholder is never an identity, whatever shape it was written in.

Measured 2026-09-22: an auth.json whose only profile was ``token_type: local`` with the
``aither_root_local`` bearer (no ``is_local_root`` flag) made ``adk rc`` think it was
signed in, and enrolment died on an opaque Identity 401 instead of "run adk login".
"""

import json

import pytest
from adk import fleet_enroll


@pytest.fixture
def auth_file(tmp_path, monkeypatch):
    path = tmp_path / "auth.json"
    monkeypatch.setattr(fleet_enroll, "_AUTH_FILE", path)
    return path


def _write(path, data):
    path.write_text(json.dumps(data), encoding="utf-8")


@pytest.mark.parametrize(
    "profile",
    [
        {"is_local_root": True, "access_token": "x"},
        {"token_type": "local", "access_token": "aither_root_local"},
        {"endpoint": "local", "access_token": "aither_root_local"},
    ],
)
def test_local_root_profile_is_not_an_identity(auth_file, profile):
    _write(auth_file, {"version": 1, "active_profile": "local", "profiles": {"local": profile}})
    assert fleet_enroll._load_auth_config() == {}


def test_legacy_flat_local_root_is_not_an_identity(auth_file):
    _write(auth_file, {"access_token": "aither_root_local"})
    assert fleet_enroll._load_auth_config() == {}


def test_real_cloud_profile_passes(auth_file):
    profile = {
        "token_type": "bearer", "access_token": "eyJ.a.b",
        "user": {"tenant_slug": "aitherium"},
    }
    _write(auth_file, {"version": 1, "active_profile": "cloud", "profiles": {"cloud": profile}})
    flat = fleet_enroll._load_auth_config()
    assert flat["access_token"] == "eyJ.a.b"
    assert flat["tenant_slug"] == "aitherium"
