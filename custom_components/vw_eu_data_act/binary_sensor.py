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
from .data import CURATED_BINARY, CuratedBinary, DataPoint
from .entity import EudaEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EudaConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data.coordinator
    points: dict[str, DataPoint] = coordinator.data or {}
    present_fields = {dp.field_name for dp in points.values()}

    async_add_entities(
        EudaBinarySensor(coordinator, curated)
        for curated in CURATED_BINARY
        if curated.field_name in present_fields
    )


def _find_by_field(points: dict[str, DataPoint], field_name: str) -> DataPoint | None:
    """Pick a single point for a (possibly duplicated) field name.

    See sensor._find_by_field: the smallest UUID is chosen for a stable,
    deterministic selection across refreshes.
    """
    matches = [dp for dp in points.values() if dp.field_name == field_name]
    return min(matches, key=lambda dp: dp.key) if matches else None


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
        dp = _find_by_field(self.coordinator.data or {}, self._curated.field_name)
        result = None
        if dp is not None and isinstance(dp.value, bool):
            result = (not dp.value) if self._curated.invert else dp.value
        return self._sticky(result)
