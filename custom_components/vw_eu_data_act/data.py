"""Pure-Python data layer: dataset parsing, value typing, curated registry.

This module intentionally has **no Home Assistant imports** so the parsing and
mapping logic can be unit-tested offline. Platform modules translate the plain
string device-class / unit values here into HA enums.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

# ---------------------------------------------------------------------------
# Data dictionary (generated from the PDF by tools/parse_dictionary.py)
# ---------------------------------------------------------------------------

_DICT_PATH = Path(__file__).parent / "data_dictionary.json"


@lru_cache(maxsize=1)
def load_dictionary() -> dict[str, dict[str, str]]:
    """Return { key-uuid: {name, description, unit, type, cluster} }."""
    try:
        return json.loads(_DICT_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


# ---------------------------------------------------------------------------
# Value typing
# ---------------------------------------------------------------------------

_DURATION_RE = re.compile(r"^(-?\d+(?:\.\d+)?)\s*s$", re.I)
_INT_RE = re.compile(r"^-?\d+$")
_FLOAT_RE = re.compile(r"^-?\d+\.\d+$")


def parse_duration_seconds(raw: str) -> float | None:
    """Parse values like "0s" / "1800s" into seconds."""
    m = _DURATION_RE.match(raw.strip())
    return float(m.group(1)) if m else None


def parse_value(raw: str | None, type_hint: str | None = None):
    """Coerce a raw string value into a typed Python value.

    ``type_hint`` comes from the data dictionary ("int", "float", "boolean",
    "enum", "string"). Falls back to structural detection so it works even
    without a dictionary entry.
    """
    if raw is None:
        return None
    s = raw.strip()
    if s == "":
        return None

    hint = (type_hint or "").lower()

    if hint == "boolean" or s.lower() in ("true", "false"):
        return s.lower() == "true"

    if hint in ("int", "integer") and _INT_RE.match(s):
        return int(s)
    if hint == "float":
        try:
            return float(s)
        except ValueError:
            return s

    # duration shorthand ("0s")
    dur = parse_duration_seconds(s)
    if dur is not None:
        return dur

    # structural fallbacks
    if _INT_RE.match(s):
        return int(s)
    if _FLOAT_RE.match(s):
        return float(s)

    return s  # enums, ISO timestamps, free text stay as strings


# ---------------------------------------------------------------------------
# Dataset model
# ---------------------------------------------------------------------------


@dataclass
class DataPoint:
    key: str
    field_name: str
    raw_value: str
    type_hint: str | None = None
    unit: str | None = None
    description: str | None = None
    cluster: str | None = None

    @property
    def value(self):
        return parse_value(self.raw_value, self.type_hint)


@dataclass
class Dataset:
    """A parsed dataset JSON, enriched from the data dictionary."""

    vin: str
    user_id: str | None
    points: dict[str, DataPoint] = field(default_factory=dict)  # by key
    captured_at: datetime | None = None

    @classmethod
    def from_json(cls, payload: dict) -> "Dataset":
        dictionary = load_dictionary()
        points: dict[str, DataPoint] = {}
        captured: list[datetime] = []
        for item in payload.get("Data", []):
            key = item.get("key")
            if not key:
                continue
            meta = dictionary.get(key, {})
            field_name = item.get("dataFieldName") or meta.get("name") or key
            dp = DataPoint(
                key=key,
                field_name=field_name,
                raw_value=item.get("value", ""),
                type_hint=meta.get("type") or None,
                unit=meta.get("unit") or None,
                description=meta.get("description") or None,
                cluster=meta.get("cluster") or None,
            )
            points[key] = dp
            if field_name == "car_captured_time":
                ts = _parse_timestamp(dp.raw_value)
                if ts:
                    captured.append(ts)
        return cls(
            vin=payload.get("vin", ""),
            user_id=payload.get("user_id"),
            points=points,
            captured_at=max(captured) if captured else None,
        )

    def by_field(self, field_name: str) -> DataPoint | None:
        """Return a single data point for a (possibly duplicated) field name.

        The portal merges several report snapshots into one flat array with no
        ordering guarantee and no way to tell which value is "live", so a field
        like ``charging_state_report.current_charge_state`` can appear several
        times under different UUIDs with conflicting values. We pick the entry
        with the smallest ``key`` (UUID): an arbitrary but *stable* choice, so a
        curated sensor consistently tracks the same data point across refreshes
        instead of flip-flopping when the portal reshuffles the array.
        """
        matches = [dp for dp in self.points.values() if dp.field_name == field_name]
        return min(matches, key=lambda dp: dp.key) if matches else None


def _parse_timestamp(raw: str) -> datetime | None:
    """Parse the various timestamp encodings seen in datasets."""
    s = (raw or "").strip()
    if not s:
        return None
    # epoch millis
    if _INT_RE.match(s) and len(s) >= 12:
        try:
            return datetime.fromtimestamp(int(s) / 1000, tz=timezone.utc)
        except (ValueError, OSError):
            return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Curated entity registry  (plain strings -> translated to HA enums in platforms)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CuratedSensor:
    field_name: str
    name: str
    device_class: str | None = None
    unit: str | None = None
    state_class: str | None = None
    icon: str | None = None
    # transform: "duration_s" converts "0s" -> seconds; None keeps parse_value
    transform: str | None = None


@dataclass(frozen=True)
class CuratedBinary:
    field_name: str
    name: str
    device_class: str | None = None
    invert: bool = False  # is_on = (value is False) when True
    icon: str | None = None


# device_class / unit / state_class strings equal HA's StrEnum values.
CURATED_SENSORS: tuple[CuratedSensor, ...] = (
    CuratedSensor("battery_state_report.soc", "Battery", "battery", "%", "measurement"),
    CuratedSensor("settings.target_soc", "Target charge level", None, "%", "measurement", icon="mdi:battery-charging-80"),
    CuratedSensor("battery_state_report.charge_bulk_threshold", "Charge bulk threshold", None, "%", "measurement", icon="mdi:battery-charging-100"),
    CuratedSensor("battery_state_report.charge_power", "Charge power", "power", "kW", "measurement"),
    CuratedSensor("mileage.value", "Mileage", "distance", "km", "total_increasing", icon="mdi:counter"),
    CuratedSensor("min_temperature", "Climate min temperature", "temperature", "°C", "measurement"),
    CuratedSensor("max_temperature", "Climate max temperature", "temperature", "°C", "measurement"),
    CuratedSensor("remaining_climate_time", "Remaining climate time", "duration", "s", "measurement", transform="duration_s"),
    CuratedSensor("range", "Range", "distance", "km", "measurement", icon="mdi:map-marker-distance"),
    CuratedSensor("scr_range", "SCR range", "distance", "km", "measurement", icon="mdi:map-marker-distance"),
    CuratedSensor("residual_energy_in_percent", "Residual energy", None, "%", "measurement", icon="mdi:battery"),
    # enum / text status sensors
    CuratedSensor("charging_state_report.current_charge_state", "Charge state", icon="mdi:ev-station"),
    CuratedSensor("charging_state_report.charge_mode", "Charge mode", icon="mdi:ev-station"),
    CuratedSensor("charging_state_report.charging_scenario", "Charging scenario", icon="mdi:ev-station"),
    CuratedSensor("charging_state_report.immediate_action_state", "Charging action state", icon="mdi:ev-station"),
    CuratedSensor("settings.charge_mode_selection", "Charge mode selection", icon="mdi:cog"),
    CuratedSensor("settings.max_charge_current_ac", "Max AC charge current", icon="mdi:current-ac"),
    CuratedSensor("window_heating_state", "Window heating", icon="mdi:car-defrost-rear"),
)

CURATED_BINARY: tuple[CuratedBinary, ...] = (
    # value "true" == locked; HA LOCK device class: on == unlocked -> invert.
    CuratedBinary("locked", "Doors locked", "lock", invert=True),
    CuratedBinary("parking_brake", "Parking brake", None, icon="mdi:car-brake-parking"),
)

CURATED_FIELDS: frozenset[str] = frozenset(
    [s.field_name for s in CURATED_SENSORS] + [b.field_name for b in CURATED_BINARY]
)
