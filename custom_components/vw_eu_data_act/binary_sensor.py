"""Binary sensor platform: curated boolean data points."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant, callback
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

    # A curated field may be missing from the dataset current at startup and
    # only turn up in a later one. The coordinator merges each dataset into its
    # data, so add entities for any newly-seen field on every refresh rather
    # than only from the first dataset (see sensor.py for the same pattern).
    added: set[str] = set()

    @callback
    def _add_new_entities() -> None:
        points: dict[str, DataPoint] = coordinator.data or {}

        # Detect dataset format and select appropriate curated group
        format_type = detect_dataset_format(points)
        curated_binary = (
            CURATED_BINARY_DOTTED if format_type == "dotted" else CURATED_BINARY_FLAT
        )

        entities = []
        for curated in curated_binary:
            if curated.field_name in added:
                continue
            # Presence in the dataset is not enough: a vehicle without a
            # sunroof, spoiler or service hatch reports those fields every
            # cycle carrying only the "unsupported"/"invalid" sentinel, which
            # decodes to None. Skip until the field decodes to a real state;
            # this loop re-runs on every refresh, so nothing is lost if the
            # reading only appears later.
            dp = find_by_field(points, curated.field_name)
            if dp is None:
                continue
            if decode_binary_state(dp.value, curated.encoding, curated.invert) is None:
                continue
            entities.append(EudaBinarySensor(coordinator, curated))
            added.add(curated.field_name)

        if entities:
            async_add_entities(entities)

    _add_new_entities()
    entry.async_on_unload(coordinator.async_add_listener(_add_new_entities))


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
