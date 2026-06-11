"""Sensor platform: curated sensors + raw diagnostic data points."""

from __future__ import annotations

import re

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import EudaConfigEntry
from .const import raw_unique_id
from .coordinator import EudaCoordinator
from .data import (
    CURATED_BINARY_DOTTED,
    CURATED_BINARY_FLAT,
    CURATED_SENSORS_DOTTED,
    CURATED_SENSORS_FLAT,
    UNIT_RESOLVERS,
    CuratedSensor,
    DataPoint,
    detect_dataset_format,
    find_by_field,
    friendly_name,
    resolve_distance_unit,
)
from .entity import EudaEntity


def _shorten_enum_value(dp: DataPoint, value) -> object:
    """Shorten verbose VW enum labels for display only.

    Keeps DataPoint.raw_value unchanged. Removes enum prefixes that are
    repeated in the field name, e.g. for ``charging_state_report.current_charge_state``
    the value ``CHARGE_STATE_CHARGING_HV_BATTERY`` becomes ``CHARGING_HV_BATTERY``.
    """
    if dp is None or not isinstance(value, str):
        return value

    if not re.fullmatch(r"[A-Z0-9_]+", value):
        return value

    def normalize(text: str) -> str:
        return re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_").upper()

    candidates: list[str] = []

    def add_candidate(text: str) -> None:
        normalized = normalize(text)
        if normalized and normalized not in candidates:
            candidates.append(normalized)

    field_name = dp.field_name or ""
    add_candidate(field_name)
    for part in field_name.split("."):
        add_candidate(part)

    normalized_field = normalize(field_name)
    for removable in ("SETTINGS_", "STATUS_", "CHARGING_STATE_REPORT_"):
        if normalized_field.startswith(removable):
            add_candidate(normalized_field.removeprefix(removable))

    for candidate in list(candidates):
        tokens = candidate.split("_")
        for i in range(1, len(tokens)):
            add_candidate("_".join(tokens[i:]))

    for prefix in sorted(candidates, key=len, reverse=True):
        full_prefix = f"{prefix}_"
        if value.startswith(full_prefix) and len(value) > len(full_prefix):
            return value[len(full_prefix):]

    return value


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EudaConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data.coordinator
    points: dict[str, DataPoint] = coordinator.data or {}
    present_fields = {dp.field_name for dp in points.values()}

    # Detect dataset format and select appropriate curated group
    format_type = detect_dataset_format(points)
    curated_sensors = (
        CURATED_SENSORS_DOTTED if format_type == "dotted" else CURATED_SENSORS_FLAT
    )
    curated_binary = (
        CURATED_BINARY_DOTTED if format_type == "dotted" else CURATED_BINARY_FLAT
    )

    # Build field sets for exclusion from raw sensors
    binary_fields = {b.field_name for b in curated_binary}
    curated_sensor_fields = {s.field_name for s in curated_sensors}

    entities: list[SensorEntity] = []

    # curated numeric / text sensors (one per field, if present)
    for curated in curated_sensors:
        # Special handling for timestamp sensors (e.g., "mileage.timestamp" or "mileage.value.timestamp")
        if ".timestamp" in curated.field_name:
            base_field = curated.field_name.replace(".timestamp", "")
            if base_field in present_fields:
                entities.append(EudaCuratedSensor(coordinator, curated))
        elif curated.field_name in present_fields:
            entities.append(EudaCuratedSensor(coordinator, curated))

    # raw diagnostic sensors: every other unique key
    for key, dp in points.items():
        if dp.field_name in curated_sensor_fields or dp.field_name in binary_fields:
            continue
        entities.append(EudaRawSensor(coordinator, key))

    async_add_entities(entities)


class EudaCuratedSensor(EudaEntity, SensorEntity):
    """A curated, well-typed sensor (enabled by default)."""

    def __init__(self, coordinator: EudaCoordinator, curated: CuratedSensor) -> None:
        super().__init__(coordinator)
        self._curated = curated
        self._attr_unique_id = f"{coordinator.vin}_{curated.field_name}"
        self._attr_name = curated.name
        if curated.icon:
            self._attr_icon = curated.icon
        if curated.device_class:
            self._attr_device_class = SensorDeviceClass(curated.device_class)
        if curated.state_class:
            self._attr_state_class = SensorStateClass(curated.state_class)
        if curated.suggested_display_precision is not None:
            self._attr_suggested_display_precision = curated.suggested_display_precision

    @property
    def native_value(self):
        # Special handling for timestamp fields (both "mileage.timestamp" and "mileage.value.timestamp")
        if ".timestamp" in self._curated.field_name:
            base_field = self._curated.field_name.replace(".timestamp", "")
            dp = find_by_field(self.coordinator.data or {}, base_field)
            if dp and dp.timestamp:
                return self._sticky(dp.timestamp)
            return self._sticky(None)

        dp = find_by_field(self.coordinator.data or {}, self._curated.field_name)

        if not dp:
            return self._sticky(None)

        raw_value = dp.value

        # Apply transforms if specified
        if self._curated.transform:
            if self._curated.transform == "decikelvin_to_celsius":
                from .data import decikelvin_to_celsius

                transformed = decikelvin_to_celsius(dp.raw_value)
                return self._sticky(transformed)

            elif self._curated.transform == "abs":
                from .data import abs_value

                transformed = abs_value(raw_value)
                return self._sticky(transformed)

            elif self._curated.transform == "fuel_consumption":
                from .data import fuel_consumption_l_per_1000km_to_l_per_100km

                transformed = fuel_consumption_l_per_1000km_to_l_per_100km(raw_value)
                return self._sticky(transformed)

        return self._sticky(_shorten_enum_value(dp, raw_value))

    @property
    def native_unit_of_measurement(self) -> str | None:
        # When a companion unit field is declared (e.g. mileage.unit), resolve
        # the unit at runtime so miles vs km is reported correctly per vehicle;
        # otherwise use the static curated unit.
        cur = self._curated
        if cur.unit_field:
            dp = find_by_field(self.coordinator.data or {}, cur.unit_field)
            if dp is not None:
                resolver = UNIT_RESOLVERS.get(cur.unit_resolver, resolve_distance_unit)
                resolved = resolver(dp.value)
                if resolved:
                    return resolved
        return cur.unit


class EudaRawSensor(EudaEntity, SensorEntity):
    """A raw data point exposed as a disabled-by-default diagnostic sensor."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: EudaCoordinator, key: str) -> None:
        super().__init__(coordinator)
        dp = coordinator.data[key]
        self._key = key
        # Namespace by VIN: dataset keys are shared across vehicles, so a bare
        # key collides between config entries (see raw_unique_id / migration).
        self._attr_unique_id = raw_unique_id(coordinator.vin, key)
        self._attr_name = friendly_name(dp.field_name, dp.description)
        # only attach a unit when the value is numeric
        if dp.unit and dp.type_hint in ("int", "float"):
            self._attr_native_unit_of_measurement = dp.unit

    @property
    def native_value(self):
        dp = (self.coordinator.data or {}).get(self._key)
        return self._sticky(_shorten_enum_value(dp, dp.value) if dp else None)

    @property
    def extra_state_attributes(self) -> dict:
        dp = (self.coordinator.data or {}).get(self._key)
        if not dp:
            return {}
        attrs = {"key": dp.key, "field_name": dp.field_name}
        if dp.description:
            attrs["description"] = dp.description
        if dp.cluster:
            attrs["cluster"] = dp.cluster
        return attrs
