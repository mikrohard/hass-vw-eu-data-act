"""Shared pytest setup for the HA-dependent tests.

Puts the repo root on the path so the integration imports under its real
package name (``custom_components.vw_eu_data_act``) and loads the Home Assistant
test harness.

Note: the HA harness (pytest-homeassistant-custom-component) imports Unix-only
modules, so ``pytest`` runs on Linux/macOS or in CI. On Windows, run the
HA-independent suite directly: ``python tests/test_offline.py``.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

pytest_plugins = ["pytest_homeassistant_custom_component"]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Allow HA to load the bundled custom integration during tests."""
    yield
