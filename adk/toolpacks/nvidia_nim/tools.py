"""NVIDIA NIM (hosted build.nvidia.com API) tool pack — tool implementations.

Auto-generated from the component spec. Do not edit by hand: run the generator
and let --check report drift.
"""
from __future__ import annotations

import logging

from . import config

logger = logging.getLogger("nim_pack")


def _guard() -> dict | None:
    """Return the not-authenticated envelope, or None when a key is present."""
    if config.api_key():
        return None
    return {"error": "not_authenticated", "fix": config.FIX}


def nim_list_models() -> dict:
    """List the models served by the hosted NIM API."""
    denied = _guard()
    if denied:
        return denied
    return config.request("GET", "/models", None, config.api_key())


def nim_chat(prompt, model="meta/llama-3.1-8b-instruct", max_tokens=512, temperature=0.2) -> dict:
    """One chat completion against a hosted NIM model."""
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


def nim_check_health() -> dict:
    """Confirm the hosted endpoint answers with the current key."""
    denied = _guard()
    if denied:
        return denied
    return config.request("GET", "/models", None, config.api_key())
