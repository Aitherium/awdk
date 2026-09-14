"""Tests for adk doctor command."""

from __future__ import annotations

import sys
from unittest.mock import patch, MagicMock

import pytest


def test_check_python():
    from adk.doctor import check_python

    # Current Python should pass (we're running >= 3.10)
    with patch("builtins.print"):
        assert check_python() is True


def test_check_version():
    from adk.doctor import check_version

    with patch("builtins.print"):
        assert check_version() is True


def test_check_disk():
    from adk.doctor import check_disk

    with patch("builtins.print"):
        assert check_disk() is True


def test_cmd_doctor_runs():
    from adk.doctor import cmd_doctor

    with patch("builtins.print"):
        result = cmd_doctor()
    assert result == 0


def test_check_ollama_not_installed():
    from adk.doctor import check_ollama

    with patch("shutil.which", return_value=None), patch("builtins.print"):
        ok, models = check_ollama()
    assert ok is False
    assert models == []


def test_check_docker_not_installed():
    from adk.doctor import check_docker

    with patch("shutil.which", return_value=None), patch("builtins.print"):
        assert check_docker() is False
