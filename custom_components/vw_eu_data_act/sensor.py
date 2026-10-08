"""Sensor platform: curated sensors + raw diagnostic data points."""

from __future__ import annotations

import re

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
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
    curated_has_reading,
    detect_dataset_format,
    find_by_field,
    normalize_unit,
    parse_timestamp,
    raw_entity_name,
    resolve_distance_unit,
    tenths_to_units,
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

    # A field may be absent from the dataset that happens to be current at
    # startup and only appear in a later one (e.g. SOC / mileage aren't in
    # every snapshot). The coordinator merges each dataset into its data, so we
    # add entities for any newly-seen field on every refresh rather than only
    # from the first dataset — otherwise those sensors would never be created.
    added_curated: set[str] = set()
    added_raw: set[str] = set()

    @callback
    def _add_new_entities() -> None:
        points: dict[str, DataPoint] = coordinator.data or {}

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
            if curated.field_name in added_curated:
                continue
            # Special handling for timestamp sensors (e.g., "mileage.timestamp" or "mileage.value.timestamp")
            if ".timestamp" in curated.field_name:
                base_field = curated.field_name.replace(".timestamp", "")
                base_dp = find_by_field(points, base_field)
                if base_dp is not None and base_dp.timestamp is not None:
                    entities.append(EudaCuratedSensor(coordinator, curated))
                    added_curated.add(curated.field_name)
                continue

            # Being listed in the dataset is not enough: a vehicle without the
            # hardware reports the field every cycle with no value or a
            # sentinel. Waiting for a real reading keeps those entities out of
            # the registry entirely instead of showing them permanently empty.
            # This loop re-runs on every refresh, so the entity still appears
            # the moment a usable value turns up.
            dp = find_by_field(points, curated.field_name)
            if dp is not None and curated_has_reading(dp, curated):
                entities.append(EudaCuratedSensor(coordinator, curated))
                added_curated.add(curated.field_name)

        # raw diagnostic sensors: every other unique key
        for key, dp in points.items():
            if key in added_raw:
                continue
            if dp.field_name in curated_sensor_fields or dp.field_name in binary_fields:
                continue
            entities.append(EudaRawSensor(coordinator, key))
            added_raw.add(key)

        if entities:
            async_add_entities(entities)

    # One diagnostic sensor answers "which portal ZIP is HA showing?" without
    # an attribute on every entity (which would write a state row per entity
    # per refresh).
    async_add_entities([EudaLastDatasetSensor(coordinator)])

    _add_new_entities()
    entry.async_on_unload(coordinator.async_add_listener(_add_new_entities))


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
        if curated.diagnostic:
            self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self):
        # car_captured_time appears in many report clusters; Dataset.from_json
        # already picks the latest value as captured_at on the coordinator.
        if self._curated.field_name == "car_captured_time":
            return self._sticky(self.coordinator.captured_at)

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

        # Drop protocol sentinels ("unsupported" / "invalid") before anything
        # else, so they can never be transformed into a plausible measurement.
        if self._curated.sentinels:
            from .data import strip_sentinel

            raw_value = strip_sentinel(raw_value, self._curated.sentinels)
            if raw_value is None:
                return self._sticky(None)

        # Apply transforms if specified
        if self._curated.transform:
            if self._curated.transform == "decikelvin_to_celsius":
                from .data import decikelvin_to_celsius

                transformed = decikelvin_to_celsius(dp.raw_value)
                return self._sticky(transformed)

            elif self._curated.transform == "service_interval":
                from .data import service_interval_remaining

                transformed = service_interval_remaining(raw_value)
                return self._sticky(transformed)

            elif self._curated.transform == "fuel_consumption":
                from .data import fuel_consumption_l_per_1000km_to_l_per_100km

                transformed = fuel_consumption_l_per_1000km_to_l_per_100km(raw_value)
                return self._sticky(transformed)

            elif self._curated.transform == "tenths":
                return self._sticky(tenths_to_units(raw_value))

            elif self._curated.transform == "timestamp":
                # ISO strings stay strings in parse_value; a timestamp device
                # class needs a tz-aware datetime.
                return self._sticky(parse_timestamp(dp.raw_value))

            elif self._curated.transform == "charging_time":
                from .data import strip_charging_time_sentinel

                transformed = strip_charging_time_sentinel(raw_value)
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


class EudaLastDatasetSensor(EudaEntity, SensorEntity):
    """Diagnostic: filename of the portal ZIP the current data came from."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:folder-zip-outline"
    _attr_name = "Last dataset"

    def __init__(self, coordinator: EudaCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.vin}_last_dataset"

    @property
    def native_value(self) -> str | None:
        return self.coordinator.latest_dataset_name


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
        self._attr_name = raw_entity_name(dp.field_name, dp.description)
        # Only attach a unit when the value is numeric and the dictionary names
        # a unit the raw value is already expressed in; see normalize_unit.
        if dp.type_hint in ("int", "float"):
            unit, device_class = normalize_unit(dp.unit)
            if unit:
                self._attr_native_unit_of_measurement = unit
                self._attr_state_class = SensorStateClass.MEASUREMENT
                if device_class:
                    self._attr_device_class = SensorDeviceClass(device_class)

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
