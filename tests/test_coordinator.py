"""Coordinator tests: authentication failures surface as ConfigEntryAuthFailed.

An expired/invalid login must trigger Home Assistant's reauth flow, not be
swallowed as a transient polling error. AuthError is a subclass of ApiError, so
the coordinator has to catch it *before* the generic ApiError handling in both
the dataset-listing and dataset-download paths.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vw_eu_data_act.api import ApiError, AuthError
from custom_components.vw_eu_data_act.const import (
    CONF_IDENTIFIER,
    CONF_VIN,
    DOMAIN,
)
from custom_components.vw_eu_data_act.coordinator import EudaCoordinator


def _make_coordinator(hass, client) -> EudaCoordinator:
    """Build a coordinator the way async_setup_entry does."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_VIN: "WVWZZZTESTVIN0001", CONF_IDENTIFIER: "ident-1"},
        unique_id="WVWZZZTESTVIN0001",
    )
    entry.add_to_hass(hass)
    return EudaCoordinator(hass, entry, client)


async def test_auth_error_while_listing_raises_reauth(hass) -> None:
    client = MagicMock()
    client.async_list_datasets = AsyncMock(side_effect=AuthError("invalid token"))
    coordinator = _make_coordinator(hass, client)

    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()


async def test_auth_error_while_downloading_raises_reauth(hass) -> None:
    # Listing succeeds, but the download leg hits an expired session. Because
    # AuthError subclasses ApiError, this previously fell into the retry/skip
    # branch instead of triggering reauth.
    client = MagicMock()
    client.async_list_datasets = AsyncMock(
        return_value=[
            {"name": "WVWZZZTESTVIN0001_20260101000000.zip", "createdOn": "2026-01-01T00:00:00Z"}
        ]
    )
    client.async_download_dataset = AsyncMock(side_effect=AuthError("session expired"))
    coordinator = _make_coordinator(hass, client)

    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()


async def test_plain_api_error_does_not_raise_reauth(hass) -> None:
    # A generic (non-auth) failure on first load must surface as a normal
    # UpdateFailed, never as a reauth trigger. A 400 ("data delivery not ready")
    # is not retried, so this stays fast and deterministic.
    client = MagicMock()
    client.async_list_datasets = AsyncMock(side_effect=ApiError("HTTP 400", status=400))
    client.async_get_metadata = AsyncMock(side_effect=ApiError("no metadata", status=400))
    coordinator = _make_coordinator(hass, client)

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()
