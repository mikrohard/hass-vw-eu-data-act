"""Binary sensor platform: curated boolean data points."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import EudaConfigEntry
from .coordinator import EudaCoordinator
from .data import (
    CURATED_BINARY_DOTTED,
    CURATED_BINARY_FLAT,
    CuratedBinary,
    DataPoint,
    decode_binary_state,
    detect_dataset_format,
    find_by_field,
)
from .entity import EudaEntity


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
    curated_binary = (
        CURATED_BINARY_DOTTED if format_type == "dotted" else CURATED_BINARY_FLAT
    )

    async_add_entities(
        EudaBinarySensor(coordinator, curated)
        for curated in curated_binary
        if curated.field_name in present_fields
    )


class EudaBinarySensor(EudaEntity, BinarySensorEntity):
    """A curated boolean sensor."""

    def __init__(self, coordinator: EudaCoordinator, curated: CuratedBinary) -> None:
        super().__init__(coordinator)
        self._curated = curated
        self._attr_unique_id = f"{coordinator.vin}_{curated.field_name}"
        self._attr_name = curated.name
        if curated.icon:
            self._attr_icon = curated.icon
        if curated.device_class:
            self._attr_device_class = BinarySensorDeviceClass(curated.device_class)

    @property
    def is_on(self) -> bool | None:
        dp = find_by_field(self.coordinator.data or {}, self._curated.field_name)
        value = dp.value if dp is not None else None
        result = decode_binary_state(
            value, self._curated.encoding, self._curated.invert
        )
        return self._sticky(result)
