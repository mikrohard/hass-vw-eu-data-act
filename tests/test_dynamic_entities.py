"""Platform setup adds entities for fields that appear after startup.

The portal doesn't put every field in every snapshot, so a sensor like SOC or
mileage can be absent from whichever dataset happens to be current when Home
Assistant starts and only turn up in a later one. The coordinator merges each
dataset into its data; the platforms must create entities for newly-seen fields
on refresh, otherwise those sensors would never exist for the whole HA session.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vw_eu_data_act import EudaRuntimeData
from custom_components.vw_eu_data_act import binary_sensor as binary_sensor_platform
from custom_components.vw_eu_data_act import sensor as sensor_platform
from custom_components.vw_eu_data_act.const import CONF_IDENTIFIER, CONF_VIN, DOMAIN
from custom_components.vw_eu_data_act.coordinator import EudaCoordinator
from custom_components.vw_eu_data_act.data import DataPoint


def _dp(field_name: str, raw_value: str = "1", **kw) -> DataPoint:
    return DataPoint(key=field_name, field_name=field_name, raw_value=raw_value, **kw)


def _make_coordinator(hass) -> EudaCoordinator:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_VIN: "WVWZZZE1ZLP010257", CONF_IDENTIFIER: "ident-1"},
        unique_id="WVWZZZE1ZLP010257",
    )
    entry.add_to_hass(hass)
    coordinator = EudaCoordinator(hass, entry, MagicMock())
    entry.runtime_data = EudaRuntimeData(coordinator=coordinator, session=MagicMock())
    return coordinator


async def test_sensor_appears_on_later_dataset(hass) -> None:
    coordinator = _make_coordinator(hass)
    # Startup dataset: no SOC, no mileage (mirrors 20260703154236 in the wild).
    coordinator.data = {
        "charging_state_report.current_charge_state": _dp(
            "charging_state_report.current_charge_state", "CHARGE_STATE_OFF"
        ),
    }

    added: list = []
    await sensor_platform.async_setup_entry(
        hass, coordinator.entry, lambda ents: added.extend(ents)
    )

    names_before = {getattr(e, "_attr_unique_id", None) for e in added}
    assert f"{coordinator.vin}_battery_state_report.soc" not in names_before

    # A later dataset brings SOC and mileage; coordinator merges them in and
    # notifies listeners.
    coordinator.async_set_updated_data(
        {
            **coordinator.data,
            "battery_state_report.soc": _dp(
                "battery_state_report.soc", "80", type_hint="int", unit="%"
            ),
            "mileage.value": _dp("mileage.value", "12345", type_hint="int"),
        }
    )
    await hass.async_block_till_done()

    unique_ids = {getattr(e, "_attr_unique_id", None) for e in added}
    assert f"{coordinator.vin}_battery_state_report.soc" in unique_ids
    assert f"{coordinator.vin}_mileage.value" in unique_ids


async def test_no_duplicate_entities_when_field_persists(hass) -> None:
    coordinator = _make_coordinator(hass)
    coordinator.data = {
        "battery_state_report.soc": _dp(
            "battery_state_report.soc", "50", type_hint="int", unit="%"
        ),
    }

    added: list = []
    await sensor_platform.async_setup_entry(
        hass, coordinator.entry, lambda ents: added.extend(ents)
    )

    # Same field present again on the next refresh must not create a second entity.
    coordinator.async_set_updated_data(
        {
            "battery_state_report.soc": _dp(
                "battery_state_report.soc", "55", type_hint="int", unit="%"
            ),
        }
    )
    await hass.async_block_till_done()

    soc_ids = [
        e
        for e in added
        if getattr(e, "_attr_unique_id", None)
        == f"{coordinator.vin}_battery_state_report.soc"
    ]
    assert len(soc_ids) == 1


async def test_binary_sensor_appears_on_later_dataset(hass) -> None:
    coordinator = _make_coordinator(hass)
    # Dotted-format car, but no "locked" field at startup.
    coordinator.data = {
        "charging_state_report.current_charge_state": _dp(
            "charging_state_report.current_charge_state", "CHARGE_STATE_OFF"
        ),
    }

    added: list = []
    await binary_sensor_platform.async_setup_entry(
        hass, coordinator.entry, lambda ents: added.extend(ents)
    )
    assert not added

    coordinator.async_set_updated_data(
        {
            **coordinator.data,
            "locked": _dp("locked", "true", type_hint="boolean"),
        }
    )
    await hass.async_block_till_done()

    unique_ids = {getattr(e, "_attr_unique_id", None) for e in added}
    assert f"{coordinator.vin}_locked" in unique_ids


async def test_last_dataset_sensor_reports_zip_name(hass) -> None:
    coordinator = _make_coordinator(hass)
    coordinator.data = {}
    coordinator.latest_dataset_name = "WVWZZZE1ZLP010257_20260101000000.zip"

    added: list = []
    await sensor_platform.async_setup_entry(
        hass, coordinator.entry, lambda ents: added.extend(ents)
    )

    sensors = [
        e
        for e in added
        if getattr(e, "_attr_unique_id", None) == f"{coordinator.vin}_last_dataset"
    ]
    assert len(sensors) == 1
    assert sensors[0].native_value == "WVWZZZE1ZLP010257_20260101000000.zip"
    # Exactly one extra entity, not one attribute per entity.
    assert all("source_dataset" not in (e.extra_state_attributes or {}) for e in added)
