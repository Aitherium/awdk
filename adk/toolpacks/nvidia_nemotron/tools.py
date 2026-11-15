"""NVIDIA Nemotron Orchestrator tool pack — tool implementations.

Auto-generated from the component spec. Do not edit by hand: run the generator
and let --check report drift.
"""
from __future__ import annotations

import logging

from . import config

logger = logging.getLogger("nemotron_pack")


def _guard() -> dict | None:
    """Return the not-authenticated envelope, or None when a key is present."""
    if config.api_key():
        return None
    return {"error": "not_authenticated", "fix": config.FIX}


def nemotron_chat(
    prompt,
    model="nvidia/llama-3.3-nemotron-super-49b-v1",
    max_tokens=512,
    temperature=0.2,
) -> dict:
    """One chat completion against a hosted Nemotron model."""
    denied = _guard()
    if denied:
        return denied
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    return config.request("POST", "/chat/completions", payload, config.api_key())


def nemotron_status() -> dict:
    """Report which Nemotron models the endpoint currently lists."""
    denied = _guard()
    if denied:
        return denied
    return config.request("GET", "/models", None, config.api_key())


def nemotron_config() -> dict:
    """Report the resolved endpoint and credential source (no secret value)."""
    denied = _guard()
    if denied:
        return denied
    return {
        "status": "ok",
        "api_base": config.API_BASE,
        "credential_source": config.credential_source(),
    }
