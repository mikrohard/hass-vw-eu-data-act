"""Config-flow tests: the reauth step must restore the entry's brand.

A non-Volkswagen user (e.g. Škoda) who re-authenticates has to log in against
their own brand's OIDC client. The reauth step therefore has to read CONF_BRAND
back from the stored entry data; otherwise it falls back to the Volkswagen
default and the login fails for everyone else.
"""
from __future__ import annotations

from unittest.mock import patch

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vw_eu_data_act.config_flow import EudaConfigFlow
from custom_components.vw_eu_data_act.const import (
    CONF_BRAND,
    CONF_EMAIL,
    CONF_IDENTIFIER,
    CONF_PASSWORD,
    CONF_VIN,
    DEFAULT_BRAND,
    DOMAIN,
)

CLIENT_PATH = "custom_components.vw_eu_data_act.config_flow.EudaApiClient"


def _fake_client_capturing(captured: dict):
    """A stand-in EudaApiClient that records the brand and logs in cleanly."""

    class _FakeClient:
        def __init__(self, session, email, password, brand=DEFAULT_BRAND) -> None:
            captured["brand"] = brand

        async def async_login(self) -> None:
            return None

        async def async_list_vehicles(self) -> list[dict]:
            return [{"vin": "WVWZZZTESTVIN0001"}]

    return _FakeClient


async def test_reauth_restores_non_default_brand(hass) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_BRAND: "skoda",
            CONF_EMAIL: "owner@example.com",
            CONF_PASSWORD: "old-secret",
            CONF_VIN: "WVWZZZTESTVIN0001",
            CONF_IDENTIFIER: "ident-1",
        },
        unique_id="WVWZZZTESTVIN0001",
    )
    entry.add_to_hass(hass)

    flow = EudaConfigFlow()
    flow.hass = hass

    result = await flow.async_step_reauth(dict(entry.data))
    # The regression: brand restored from the entry, not left at the default.
    assert flow._brand == "skoda"
    assert result["type"] == "form"
    assert result["step_id"] == "reauth_confirm"

    captured: dict = {}
    with patch(CLIENT_PATH, _fake_client_capturing(captured)):
        assert await flow._async_try_login() is None
    # ...and it is the brand handed to the API client.
    assert captured["brand"] == "skoda"


async def test_reauth_defaults_brand_when_absent(hass) -> None:
    # Entries created before multi-brand support have no CONF_BRAND; they must
    # default to Volkswagen for backward compatibility.
    flow = EudaConfigFlow()
    flow.hass = hass

    await flow.async_step_reauth({CONF_EMAIL: "legacy@example.com"})
    assert flow._brand == DEFAULT_BRAND


async def test_user_step_selects_brand(hass) -> None:
    # The first step offers a brand choice and advances to the credentials step.
    flow = EudaConfigFlow()
    flow.hass = hass

    form = await flow.async_step_user(None)
    assert form["type"] == "form"
    assert form["step_id"] == "user"

    nxt = await flow.async_step_user({CONF_BRAND: "audi"})
    assert flow._brand == "audi"
    assert nxt["step_id"] == "credentials"
