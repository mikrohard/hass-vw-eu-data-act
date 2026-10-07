"""Curated ID.3 (dotted-format) additions decode real portal values correctly.

Values mirror a 2026-09-18 ID.3 dataset: energy content arrives in 0.1 kWh
steps, charging-timer times are ISO strings that must become tz-aware
datetimes for a ``timestamp`` device class, battery care mode is an enum
label rather than a boolean, and the enum sensors drop their repeated
prefix for display.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

from homeassistant.const import EntityCategory
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vw_eu_data_act import EudaRuntimeData
from custom_components.vw_eu_data_act import binary_sensor as binary_sensor_platform
from custom_components.vw_eu_data_act import sensor as sensor_platform
from custom_components.vw_eu_data_act.const import CONF_IDENTIFIER, CONF_VIN, DOMAIN
from custom_components.vw_eu_data_act.coordinator import EudaCoordinator
from custom_components.vw_eu_data_act.data import DataPoint

TIMER = "profile_state_report.next_charging_timer_information"

ID3_POINTS = {
    "additional_consumptions.interior_climatization_consumption": ("4.5", "float"),
    "additional_consumptions.residual_consumption": ("5.9", "float"),
    "battery_state_report.soc": ("56", "int"),
    "charging_state_report.profile_charge_reason": ("PROFILE_CHARGE_REASON_LOW_COST", "enum"),
    "energy_contents.current_energy_content.physical_value": ("237.5", "float"),
    "energy_contents.maximal_energy_content.physical_value": ("483.5", "float"),
    f"{TIMER}.estimated_finish_time": ("2026-09-17T22:24:00Z", "string"),
    f"{TIMER}.estimated_start_time": ("2026-09-17T20:00:00Z", "string"),
    f"{TIMER}.target_reachability": ("TARGET_REACHABILITY_REACHABLE", "enum"),
    "setting.bcam_activation": ("BCAM_ACTIVATION_ACTIVATED", "enum"),
    "settings.auto_unlock_ac": ("AUTO_UNLOCK_AC_OFF", "enum"),
    "update_reason": ("UPDATE_REASON_OTHER", "enum"),
    "slope_consumption_values.ascent_slope_consumption.physical_value": ("54.699997", "float"),
}


def _make_coordinator(hass) -> EudaCoordinator:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_VIN: "WVWZZZE1ZLP010257", CONF_IDENTIFIER: "ident-1"},
        unique_id="WVWZZZE1ZLP010257",
    )
    entry.add_to_hass(hass)
    coordinator = EudaCoordinator(hass, entry, MagicMock())
    entry.runtime_data = EudaRuntimeData(coordinator=coordinator, session=MagicMock())
    coordinator.data = {
        name: DataPoint(key=name, field_name=name, raw_value=raw, type_hint=hint)
        for name, (raw, hint) in ID3_POINTS.items()
    }
    return coordinator


async def _sensors(hass) -> tuple[EudaCoordinator, dict[str, object]]:
    coordinator = _make_coordinator(hass)
    added: list = []
    await sensor_platform.async_setup_entry(
        hass, coordinator.entry, lambda ents: added.extend(ents)
    )
    by_field = {}
    for ent in added:
        curated = getattr(ent, "_curated", None)
        if curated is not None:
            by_field[curated.field_name] = ent
    return coordinator, by_field


async def test_energy_content_is_tenths_of_kwh(hass) -> None:
    _, sensors = await _sensors(hass)
    current = sensors["energy_contents.current_energy_content.physical_value"]
    maximal = sensors["energy_contents.maximal_energy_content.physical_value"]
    assert current.native_value == 23.75
    assert maximal.native_value == 48.35
    assert current.native_unit_of_measurement == "kWh"
    assert str(current.device_class) == "energy_storage"


async def test_charging_timer_times_are_datetimes(hass) -> None:
    _, sensors = await _sensors(hass)
    start = sensors[f"{TIMER}.estimated_start_time"].native_value
    finish = sensors[f"{TIMER}.estimated_finish_time"].native_value
    assert start == datetime(2026, 9, 17, 20, 0, tzinfo=timezone.utc)
    assert finish == datetime(2026, 9, 17, 22, 24, tzinfo=timezone.utc)
    assert sensors[f"{TIMER}.target_reachability"].native_value == "REACHABLE"


async def test_enum_sensors_drop_repeated_prefix(hass) -> None:
    _, sensors = await _sensors(hass)
    assert sensors["charging_state_report.profile_charge_reason"].native_value == "LOW_COST"
    assert sensors["settings.auto_unlock_ac"].native_value == "OFF"
    assert sensors["update_reason"].native_value == "OTHER"


async def test_update_reason_is_diagnostic_but_others_are_not(hass) -> None:
    _, sensors = await _sensors(hass)
    assert sensors["update_reason"].entity_category == EntityCategory.DIAGNOSTIC
    assert sensors["battery_state_report.soc"].entity_category is None
    # curated entities stay enabled by default, unlike raw ones
    assert sensors["update_reason"].entity_registry_enabled_default is True


async def test_consumption_sensors_are_tenths(hass) -> None:
    # Raw 4.5 / 5.9 are tenths, like energy_contents in the same report; an
    # October reading of raw 8.3 would otherwise show as 8.3 kWh/100 km.
    _, sensors = await _sensors(hass)
    climate = sensors["additional_consumptions.interior_climatization_consumption"]
    residual = sensors["additional_consumptions.residual_consumption"]
    assert climate.native_value == 0.45
    assert residual.native_value == 0.59
    assert climate.native_unit_of_measurement == "kWh/100km"


async def test_slope_consumption_stays_raw(hass) -> None:
    _, sensors = await _sensors(hass)
    assert "slope_consumption_values.ascent_slope_consumption.physical_value" not in sensors


async def test_battery_care_mode_binary_from_enum_label(hass) -> None:
    coordinator = _make_coordinator(hass)
    added: list = []
    await binary_sensor_platform.async_setup_entry(
        hass, coordinator.entry, lambda ents: added.extend(ents)
    )
    bcam = next(e for e in added if e._curated.field_name == "setting.bcam_activation")
    assert bcam.is_on is True

    coordinator.async_set_updated_data(
        {
            **coordinator.data,
            "setting.bcam_activation": DataPoint(
                key="setting.bcam_activation",
                field_name="setting.bcam_activation",
                raw_value="BCAM_ACTIVATION_DEACTIVATED",
                type_hint="enum",
            ),
        }
    )
    await hass.async_block_till_done()
    assert bcam.is_on is False

    # An INVALID label is unknown, and sticky keeps the last known state.
    coordinator.async_set_updated_data(
        {
            **coordinator.data,
            "setting.bcam_activation": DataPoint(
                key="setting.bcam_activation",
                field_name="setting.bcam_activation",
                raw_value="BCAM_ACTIVATION_INVALID",
                type_hint="enum",
            ),
        }
    )
    await hass.async_block_till_done()
    assert bcam.is_on is False
