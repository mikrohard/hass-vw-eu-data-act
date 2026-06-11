"""The VW Group EU Data Act integration."""
from __future__ import annotations

from dataclasses import dataclass

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import EudaApiClient
from .const import CONF_BRAND, CONF_EMAIL, CONF_PASSWORD, CONF_VIN, DEFAULT_BRAND, raw_unique_id
from .coordinator import EudaCoordinator
from .data import load_dictionary

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR]


@dataclass
class EudaRuntimeData:
    coordinator: EudaCoordinator
    session: object


type EudaConfigEntry = ConfigEntry[EudaRuntimeData]


async def async_setup_entry(hass: HomeAssistant, entry: EudaConfigEntry) -> bool:
    """Set up VW EU Data Act from a config entry."""
    # Own session (own cookie jar — auth is cookie-based) but reuse Home
    # Assistant's shared connector so we benefit from its warm DNS cache and are
    # resilient to transient DNS hiccups. connector_owner=False so closing our
    # session never closes the shared connector.
    session = aiohttp.ClientSession(
        connector=async_get_clientsession(hass).connector,
        connector_owner=False,
        cookie_jar=aiohttp.CookieJar(),
    )
    try:
        # Warm the data-dictionary cache off the event loop (it reads a bundled
        # JSON file) so it doesn't block the loop during dataset parsing.
        await hass.async_add_executor_job(load_dictionary)

        client = EudaApiClient(session, entry.data[CONF_EMAIL], entry.data[CONF_PASSWORD], entry.data.get(CONF_BRAND, DEFAULT_BRAND))
        coordinator = EudaCoordinator(hass, entry, client)

        await coordinator.async_config_entry_first_refresh()

        entry.runtime_data = EudaRuntimeData(coordinator=coordinator, session=session)

        # Migrate pre-0.1.3 raw sensor unique_ids (bare dataset key -> VIN_key)
        # so they survive the namespacing fix that lets multiple vehicles work.
        await _async_migrate_raw_unique_ids(hass, entry)

        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception:
        # Setup failed: HA will not call async_unload_entry, so close the
        # session here to avoid leaking it (and its connector).
        await session.close()
        raise

    return True


async def _async_migrate_raw_unique_ids(hass: HomeAssistant, entry: EudaConfigEntry) -> None:
    """Prefix legacy raw-sensor unique_ids (bare dataset key) with the VIN.

    Curated sensors were always namespaced; only raw diagnostic sensors used the
    bare key, which collides across vehicles. Renaming them in the registry
    preserves the user's entity_ids/customisations across the fix.
    """
    vin = entry.data[CONF_VIN]
    prefix = f"{vin}_"

    @callback
    def _migrate(reg_entry: er.RegistryEntry) -> dict | None:
        if reg_entry.domain != "sensor":
            return None
        if reg_entry.unique_id.startswith(prefix):
            return None  # already namespaced (curated, or already migrated)
        return {"new_unique_id": raw_unique_id(vin, reg_entry.unique_id)}

    await er.async_migrate_entries(hass, entry.entry_id, _migrate)


async def async_unload_entry(hass: HomeAssistant, entry: EudaConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok and entry.runtime_data:
        await entry.runtime_data.session.close()
    return unload_ok
