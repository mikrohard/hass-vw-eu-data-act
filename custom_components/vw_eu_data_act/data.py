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


def sticky(previous, current):
    """Keep the last known value when an update omits a field.

    The portal's snapshots don't include every field every cycle; a missing
    field means "no fresh reading", not "unavailable", so we fall back to the
    previous value instead of reporting unknown.
    """
    return current if current is not None else previous


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
# Enum + naming helpers
# ---------------------------------------------------------------------------

_ENUM_TOKEN_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")

# Bare field names that are meaningless on their own; for these we name the
# entity from the dictionary description instead.
_GENERIC_FIELD_NAMES = {"value", "state", "unit", "is_set", "type", "id"}


def enum_members(description: str | None) -> list[str]:
    """Parse an ordered enum member list out of a dictionary description.

    Enum fields document their members as a comma-separated, index-ordered list
    (e.g. "IMMEDIATE_ACTION_STATE_INVALID, ..."). PDF extraction injects stray
    spaces inside the tokens, so whitespace is stripped before checking each
    token looks like an UPPER_SNAKE enum label. Returns [] for prose / non-enum
    descriptions.
    """
    if not description:
        return []
    members = [re.sub(r"\s+", "", part) for part in description.split(",")]
    members = [m for m in members if _ENUM_TOKEN_RE.match(m)]
    return members if len(members) >= 2 else []


def friendly_name(field_name: str, description: str | None = None) -> str:
    """Entity name for a raw data point.

    Dotted field names are descriptive enough as-is, but some are bare and
    meaningless ("value", "state", ...). For those, fall back to the dictionary
    description (first sentence, trimmed).
    """
    if field_name.lower() in _GENERIC_FIELD_NAMES and description:
        text = description.strip().split(".")[0].strip()
        if text:
            return text[:60]
    return field_name


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
        v = parse_value(self.raw_value, self.type_hint)
        # Enum fields occasionally deliver the raw protobuf integer index instead
        # of the label; resolve it back to the string using the documented members.
        if self.type_hint == "enum" and isinstance(v, int) and not isinstance(v, bool):
            members = enum_members(self.description)
            if 0 <= v < len(members):
                return members[v]
        return v


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


# Distance unit enums (e.g. mileage.unit) -> HA unit. The portal reports
# mileage/range in either miles or kilometres depending on the vehicle, so the
# unit must not be hardcoded; it is read from a companion "*.unit" field.
DISTANCE_UNIT_BY_ENUM: dict[str, str] = {
    "MILES": "mi",
    "MILE": "mi",
    "KM": "km",
    "KILOMETER": "km",
    "KILOMETERS": "km",
    "KILOMETRE": "km",
    "KILOMETRES": "km",
}


def resolve_distance_unit(enum_value, default: str | None = None) -> str | None:
    """Map a distance-unit enum value (e.g. "MILES") to an HA unit ("mi")."""
    if isinstance(enum_value, str):
        return DISTANCE_UNIT_BY_ENUM.get(enum_value.strip().upper(), default)
    return default


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
    # companion field holding the unit enum (e.g. "mileage.unit"); when set, the
    # sensor's unit is resolved from it at runtime, falling back to ``unit``.
    unit_field: str | None = None


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
    CuratedSensor("mileage.value", "Mileage", "distance", "km", "total_increasing", icon="mdi:counter", unit_field="mileage.unit"),
    # Per the data dictionary these are the HV battery module min/max temps,
    # not climate setpoints.
    CuratedSensor("min_temperature", "Battery min temperature", "temperature", "°C", "measurement"),
    CuratedSensor("max_temperature", "Battery max temperature", "temperature", "°C", "measurement"),
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
