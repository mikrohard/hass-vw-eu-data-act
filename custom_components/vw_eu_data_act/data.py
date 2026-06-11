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
# Dataset format detection
# ---------------------------------------------------------------------------


def detect_dataset_format(points: dict[str, "DataPoint"]) -> str:
    """Detect whether dataset uses dotted (ID.x) or flat (eGolf) naming.

    Returns "dotted" if any field name contains a dot, otherwise "flat".
    ID.x/MEB cars use dotted names (battery_state_report.soc, mileage.value),
    while pre-ID.x cars use flat names (state_of_charge, mileage).
    """
    return "dotted" if any("." in dp.field_name for dp in points.values()) else "flat"


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
    timestamp_utc: str | None = None

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

    @property
    def timestamp(self) -> datetime | None:
        """Parse the timestampUtc field into a datetime object."""
        return _parse_timestamp(self.timestamp_utc) if self.timestamp_utc else None


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
                timestamp_utc=item.get("timestampUtc") or None,
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
        """Return a single data point for a (possibly duplicated) field name."""
        return find_by_field(self.points, field_name)


def find_by_field(
    points: dict[str, "DataPoint"], field_name: str
) -> "DataPoint | None":
    """Pick a single data point for a (possibly duplicated) field name.

    The portal merges several report snapshots into one flat array with no
    ordering guarantee and no way to tell which value is "live", so a field
    like ``charging_state_report.current_charge_state`` can appear several
    times under different UUIDs with conflicting values. We pick the entry with
    the smallest ``key`` (UUID): an arbitrary but *stable* choice, so a curated
    sensor consistently tracks the same data point across refreshes instead of
    flip-flopping when the portal reshuffles the array.
    """
    matches = [dp for dp in points.values() if dp.field_name == field_name]
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


# Charge-rate unit enums (battery_state_report.charge_rate_unit) -> HA unit.
# The charge rate is expressed as range gained over time and the unit (km vs
# miles, per hour vs per minute) varies by vehicle/region, so it is read from
# the companion charge_rate_unit field rather than hardcoded.
CHARGE_RATE_UNIT_BY_ENUM: dict[str, str] = {
    "CHARGE_RATE_UNIT_KM_PER_H": "km/h",
    "CHARGE_RATE_UNIT_KM_PER_MIN": "km/min",
    "CHARGE_RATE_UNIT_MILES_PER_H": "mi/h",
    "CHARGE_RATE_UNIT_MILES_PER_MIN": "mi/min",
}


def resolve_charge_rate_unit(enum_value, default: str | None = None) -> str | None:
    """Map a charge-rate-unit enum (e.g. "CHARGE_RATE_UNIT_KM_PER_H") to "km/h"."""
    if isinstance(enum_value, str):
        return CHARGE_RATE_UNIT_BY_ENUM.get(enum_value.strip().upper(), default)
    return default


def decikelvin_to_celsius(raw: str) -> float | None:
    """Convert deci-Kelvin (e.g., "2921") to Celsius.

    Outside temperature is reported in deci-Kelvin (dK):
    - 2921 dK = 292.1 K = 19.06°C
    """
    try:
        dk = float(raw)
        kelvin = dk / 10
        celsius = kelvin - 273.15
        return round(celsius, 1)
    except (ValueError, TypeError):
        return None


def abs_value(value) -> int | float | None:
    """Return absolute value, handling negative maintenance intervals.

    Maintenance intervals can be negative (overdue). Take absolute value
    for display, as the sign indicates past-due status.
    """
    try:
        abs_val = abs(float(value))
        return int(abs_val) if abs_val == int(abs_val) else abs_val
    except (ValueError, TypeError):
        return None


def fuel_consumption_l_per_1000km_to_l_per_100km(value) -> float | None:
    """Convert fuel consumption from L/1000km to L/100km.

    The API reports fuel consumption in L/1000km (e.g., 168 L/1000km).
    Convert to standard L/100km by dividing by 10 (e.g., 16.8 L/100km).
    """
    try:
        return round(float(value) / 10, 1)
    except (ValueError, TypeError):
        return None


# Named unit resolvers selectable per curated sensor via ``unit_resolver``.
UNIT_RESOLVERS = {
    "distance": resolve_distance_unit,
    "charge_rate": resolve_charge_rate_unit,
}


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
    # which named resolver in UNIT_RESOLVERS to apply to ``unit_field``'s value.
    unit_resolver: str = "distance"
    # number of decimal places to show (None = auto)
    suggested_display_precision: int | None = None


@dataclass(frozen=True)
class CuratedBinary:
    field_name: str
    name: str
    device_class: str | None = None
    invert: bool = False  # is_on = (value is False) when True
    icon: str | None = None
    # How the field's integer value maps to on/off (see decode_binary_state):
    # "open"   - 2=active, 3=inactive, 0/1=unknown  (doors, windows, locks, …)
    # "onoff"  - 0=off, 1=on                         (parking_brake)
    # "lights" - 2=off, 3/4/5=on, 0/1=unknown        (parking_lights)
    encoding: str = "open"


def decode_binary_state(
    value, encoding: str = "open", invert: bool = False
) -> bool | None:
    """Decode a curated binary field's raw value into on / off / unknown.

    Vehicle status fields encode their boolean in several ways, selected per
    sensor via ``CuratedBinary.encoding`` rather than guessed from the field
    name at runtime:

      "open"   - 0/1 = unsupported/invalid (-> unknown); 2 = active (open /
                 locked / safe / …), 3 = inactive. The dominant encoding for
                 doors, windows, sunroofs and lock/safe states.
      "onoff"  - 0 = off, 1 = on (e.g. parking_brake).
      "lights" - 0/1 = unsupported/invalid; 2 = off; 3/4/5 = on (parking_lights).

    Plain booleans are returned as-is regardless of ``encoding``. ``invert``
    flips a decoded True/False (a "lock" sensor reads on when *un*locked); it
    never turns a known state into unknown. Returns None when the value is
    missing or carries an unsupported/invalid sentinel.
    """
    if isinstance(value, bool):
        result: bool | None = value
    elif isinstance(value, int):
        if encoding == "onoff":
            result = value == 1
        elif encoding == "lights":
            result = None if value in (0, 1) else value in (3, 4, 5)
        elif value in (0, 1):
            result = None  # unsupported / invalid sentinel
        else:  # "open": 2 = active, 3 = inactive
            result = value == 2
    else:
        result = None
    if result is None:
        return None
    return (not result) if invert else result


# ---------------------------------------------------------------------------
# Curated sensors for ID.x/MEB vehicles (dotted field names)
# ---------------------------------------------------------------------------

CURATED_SENSORS_DOTTED: tuple[CuratedSensor, ...] = (
    # === Charging & Battery ===
    CuratedSensor("battery_state_report.soc", "Battery", "battery", "%", "measurement"),
    CuratedSensor(
        "settings.target_soc",
        "Target charge level",
        None,
        "%",
        "measurement",
        icon="mdi:battery-charging-80",
    ),
    CuratedSensor(
        "battery_state_report.charge_bulk_threshold",
        "Charge bulk threshold",
        None,
        "%",
        "measurement",
        icon="mdi:battery-charging-100",
    ),
    CuratedSensor(
        "battery_state_report.charge_power",
        "Charge power",
        "power",
        "kW",
        "measurement",
    ),
    CuratedSensor(
        "battery_state_report.charge_rate",
        "Charge rate",
        None,
        "km/h",
        "measurement",
        icon="mdi:speedometer",
        unit_field="battery_state_report.charge_rate_unit",
        unit_resolver="charge_rate",
    ),
    CuratedSensor(
        "battery_state_report.charge_energy",
        "Charged energy",
        "energy",
        "kWh",
        "total_increasing",
        icon="mdi:lightning-bolt-circle",
    ),
    CuratedSensor(
        "battery_state_report.remaining_charging_time_complete",
        "Remaining charging time",
        "duration",
        "s",
        "measurement",
        transform="duration_s",
        icon="mdi:battery-clock",
    ),
    CuratedSensor(
        "battery_state_report.remaining_charging_time_bulk",
        "Remaining time to bulk",
        "duration",
        "s",
        "measurement",
        transform="duration_s",
        icon="mdi:battery-clock",
    ),
    # === Distance & Range ===
    CuratedSensor(
        "mileage.value",
        "Mileage",
        "distance",
        "km",
        "total_increasing",
        icon="mdi:counter",
        unit_field="mileage.unit",
        unit_resolver="distance",
        suggested_display_precision=0,
    ),
    CuratedSensor(
        "range.value",
        "Electric range",
        "distance",
        "km",
        "measurement",
        icon="mdi:map-marker-distance",
        unit_field="range.unit",
        unit_resolver="distance",
        suggested_display_precision=0,
    ),
    # === Climate ===
    CuratedSensor(
        "remaining_climate_time",
        "Remaining climate time",
        "duration",
        "s",
        "measurement",
        transform="duration_s",
    ),
    CuratedSensor(
        "residual_energy_in_percent",
        "Residual energy",
        None,
        "%",
        "measurement",
        icon="mdi:battery",
    ),
    # === Temperature ===
    CuratedSensor(
        "min_temperature", "Battery min temperature", "temperature", "°C", "measurement"
    ),
    CuratedSensor(
        "max_temperature", "Battery max temperature", "temperature", "°C", "measurement"
    ),
    # === Vehicle Status ===
    CuratedSensor(
        "mileage.value.timestamp",
        "Last connected",
        "timestamp",
        None,
        None,
        icon="mdi:clock",
    ),
    # === Enum/Status Sensors ===
    CuratedSensor(
        "charging_state_report.current_charge_state",
        "Charge state",
        icon="mdi:ev-station",
    ),
    CuratedSensor(
        "charging_state_report.charge_mode", "Charge mode", icon="mdi:ev-station"
    ),
    CuratedSensor(
        "charging_state_report.charge_type", "Charge type", icon="mdi:power-plug"
    ),
    CuratedSensor(
        "charging_state_report.charging_scenario",
        "Charging scenario",
        icon="mdi:ev-station",
    ),
    CuratedSensor(
        "charging_state_report.immediate_action_state",
        "Charging action state",
        icon="mdi:ev-station",
    ),
    CuratedSensor(
        "settings.charge_mode_selection", "Charge mode selection", icon="mdi:cog"
    ),
    CuratedSensor(
        "settings.max_charge_current_ac", "Max AC charge current", icon="mdi:current-ac"
    ),
    CuratedSensor(
        "window_heating_state", "Window heating", icon="mdi:car-defrost-rear"
    ),
    CuratedSensor("bem_level", "BEM level", None, None, None, icon="mdi:information"),
)

CURATED_BINARY_DOTTED: tuple[CuratedBinary, ...] = (
    # === General Lock State ===
    CuratedBinary("locked", "Vehicle locked", "lock", invert=True, icon="mdi:car-key"),
    # ID.x datasets carry a flat-named parking_brake field even though most of
    # their fields are dotted, so it belongs in the dotted group too.
    CuratedBinary(
        "parking_brake",
        "Parking brake",
        None,
        icon="mdi:car-brake-parking",
        encoding="onoff",
    ),
)

# ---------------------------------------------------------------------------
# Curated sensors for pre-ID.x vehicles (flat field names)
# ---------------------------------------------------------------------------

CURATED_SENSORS_FLAT: tuple[CuratedSensor, ...] = (
    # === Distance & Range ===
    CuratedSensor(
        "mileage",
        "Mileage",
        "distance",
        "km",
        "total_increasing",
        icon="mdi:counter",
        suggested_display_precision=0,
    ),
    CuratedSensor(
        "cruising_range_combined",
        "Range (combined)",
        "distance",
        "km",
        "measurement",
        icon="mdi:map-marker-distance",
        suggested_display_precision=0,
    ),
    CuratedSensor(
        "cruising_range_primary_engine",
        "Range (primary)",
        "distance",
        "km",
        "measurement",
        icon="mdi:gas-station",
        suggested_display_precision=0,
    ),
    CuratedSensor(
        "cruising_range_secondary_engine",
        "Range (secondary)",
        "distance",
        "km",
        "measurement",
        icon="mdi:ev-station",
        suggested_display_precision=0,
    ),
    CuratedSensor(
        "range",
        "Electric range",
        "distance",
        "km",
        "measurement",
        icon="mdi:map-marker-distance",
        suggested_display_precision=0,
    ),
    CuratedSensor(
        "scr_range",
        "SCR range",
        "distance",
        "km",
        "measurement",
        icon="mdi:map-marker-distance",
        suggested_display_precision=0,
    ),
    # === Fuel ===
    CuratedSensor(
        "fuel_level_current_level",
        "Fuel level",
        None,
        "%",
        "measurement",
        icon="mdi:gas-station",
    ),
    CuratedSensor(
        "fuel_level__accuracy",
        "Fuel level accuracy",
        None,
        None,
        None,
        icon="mdi:gauge",
    ),
    CuratedSensor(
        "cng_gas_level",
        "CNG gas level",
        None,
        "%",
        "measurement",
        icon="mdi:gas-cylinder",
    ),
    # === Temperature ===
    CuratedSensor(
        "outside_temperature",
        "Outside temperature",
        "temperature",
        "°C",
        "measurement",
        transform="decikelvin_to_celsius",
    ),
    CuratedSensor(
        "min_temperature", "Battery min temperature", "temperature", "°C", "measurement"
    ),
    CuratedSensor(
        "max_temperature", "Battery max temperature", "temperature", "°C", "measurement"
    ),
    # === Climate ===
    CuratedSensor(
        "remaining_climate_time",
        "Remaining climate time",
        "duration",
        "s",
        "measurement",
        transform="duration_s",
    ),
    CuratedSensor(
        "residual_energy_in_percent",
        "Residual energy",
        None,
        "%",
        "measurement",
        icon="mdi:battery",
    ),
    # === Tire Pressure ===
    CuratedSensor(
        "tyre_pressure_actual_front_left",
        "Tire pressure FL",
        "pressure",
        "bar",
        "measurement",
        icon="mdi:car-tire-alert",
    ),
    CuratedSensor(
        "tyre_pressure_actual_front_right",
        "Tire pressure FR",
        "pressure",
        "bar",
        "measurement",
        icon="mdi:car-tire-alert",
    ),
    CuratedSensor(
        "tyre_pressure_actual_rear_left",
        "Tire pressure RL",
        "pressure",
        "bar",
        "measurement",
        icon="mdi:car-tire-alert",
    ),
    CuratedSensor(
        "tyre_pressure_actual_rear_right",
        "Tire pressure RR",
        "pressure",
        "bar",
        "measurement",
        icon="mdi:car-tire-alert",
    ),
    CuratedSensor(
        "tyre_pressure_actual_spare_tyre",
        "Tire pressure spare",
        "pressure",
        "bar",
        "measurement",
        icon="mdi:car-tire-alert",
    ),
    CuratedSensor(
        "tyre_pressure_differential_front_left",
        "Tire pressure diff FL",
        None,
        None,
        None,
        icon="mdi:gauge",
    ),
    CuratedSensor(
        "tyre_pressure_differential_front_right",
        "Tire pressure diff FR",
        None,
        None,
        None,
        icon="mdi:gauge",
    ),
    CuratedSensor(
        "tyre_pressure_differential_rear_left",
        "Tire pressure diff RL",
        None,
        None,
        None,
        icon="mdi:gauge",
    ),
    CuratedSensor(
        "tyre_pressure_differential_rear_right",
        "Tire pressure diff RR",
        None,
        None,
        None,
        icon="mdi:gauge",
    ),
    CuratedSensor(
        "tyre_pressure_differential_spare_tyre",
        "Tire pressure diff spare",
        None,
        None,
        None,
        icon="mdi:gauge",
    ),
    # === Window Positions (0-100%) ===
    CuratedSensor(
        "position_front_left_door_window_lifter",
        "Front left window position",
        None,
        "%",
        None,
        icon="mdi:window-open-variant",
    ),
    CuratedSensor(
        "position_front_right_door_window_lifter",
        "Front right window position",
        None,
        "%",
        None,
        icon="mdi:window-open-variant",
    ),
    CuratedSensor(
        "position_rear_left_door_window_lifter",
        "Rear left window position",
        None,
        "%",
        None,
        icon="mdi:window-open-variant",
    ),
    CuratedSensor(
        "position_rear_right_door_window_lifter",
        "Rear right window position",
        None,
        "%",
        None,
        icon="mdi:window-open-variant",
    ),
    # === Sunroof ===
    CuratedSensor(
        "position_sunroof_motor_hood_1",
        "Sunroof position",
        None,
        "%",
        None,
        icon="mdi:car-convertible",
    ),
    # === Maintenance ===
    CuratedSensor(
        "maintenance_interval__time_until_inspection",
        "Inspection interval",
        None,
        "d",
        "measurement",
        icon="mdi:calendar-clock",
        transform="abs",
        suggested_display_precision=0,
    ),
    CuratedSensor(
        "maintenance_interval__time_until_oil_change",
        "Oil change interval",
        None,
        "d",
        "measurement",
        icon="mdi:oil",
        transform="abs",
        suggested_display_precision=0,
    ),
    CuratedSensor(
        "maintenance_interval_distance_until_inspection",
        "Inspection distance",
        "distance",
        "km",
        "measurement",
        icon="mdi:car-wrench",
        transform="abs",
        suggested_display_precision=0,
    ),
    CuratedSensor(
        "maintenance_interval_distance_until_oil_change",
        "Oil change distance",
        "distance",
        "km",
        "measurement",
        icon="mdi:oil",
        transform="abs",
        suggested_display_precision=0,
    ),
    # === Trip Statistics - Long Term ===
    CuratedSensor(
        "long_term_data_mileage",
        "Trip distance (long)",
        "distance",
        "km",
        "total_increasing",
        icon="mdi:map-marker-distance",
        suggested_display_precision=0,
    ),
    CuratedSensor(
        "long_term_data_start_mileage",
        "Trip start mileage (long)",
        "distance",
        "km",
        None,
        icon="mdi:counter",
        suggested_display_precision=0,
    ),
    CuratedSensor(
        "long_term_data_average_fuel_consumption",
        "Avg fuel consumption (long)",
        None,
        "L/100km",
        "measurement",
        icon="mdi:gas-station",
        transform="fuel_consumption",
        suggested_display_precision=1,
    ),
    CuratedSensor(
        "long_term_data_average_speed",
        "Avg speed (long)",
        None,
        "km/h",
        "measurement",
        icon="mdi:speedometer",
    ),
    CuratedSensor(
        "long_term_data_travel_time",
        "Travel time (long)",
        "duration",
        "min",
        "total_increasing",
        icon="mdi:clock-outline",
    ),
    # === Trip Statistics - Short Term ===
    CuratedSensor(
        "short_term_data_mileage",
        "Trip distance (short)",
        "distance",
        "km",
        "total_increasing",
        icon="mdi:map-marker-distance",
        suggested_display_precision=0,
    ),
    CuratedSensor(
        "short_term_data_start_mileage",
        "Trip start mileage (short)",
        "distance",
        "km",
        None,
        icon="mdi:counter",
        suggested_display_precision=0,
    ),
    CuratedSensor(
        "short_term_data_average_fuel_consumption",
        "Avg fuel consumption (short)",
        None,
        "L/100km",
        "measurement",
        icon="mdi:gas-station",
        transform="fuel_consumption",
        suggested_display_precision=1,
    ),
    CuratedSensor(
        "short_term_data_travel_time",
        "Travel time (short)",
        "duration",
        "min",
        "total_increasing",
        icon="mdi:clock-outline",
    ),
    # === Oil Level ===
    CuratedSensor(
        "oil_level_actual_level", "Oil level", None, "%", "measurement", icon="mdi:oil"
    ),
    CuratedSensor(
        "oil_level_additional_oil_level",
        "Additional oil level",
        None,
        "%",
        "measurement",
        icon="mdi:oil",
    ),
    CuratedSensor(
        "oil_level_total_max", "Max oil level", None, "L", None, icon="mdi:oil"
    ),
    CuratedSensor(
        "oil_level_dipstick_indicator_function",
        "Oil dipstick indicator",
        None,
        None,
        None,
        icon="mdi:gauge",
    ),
    # === Vehicle Status ===
    CuratedSensor(
        "mileage.timestamp",
        "Last connected",
        "timestamp",
        None,
        None,
        icon="mdi:clock",
    ),
    # === Enum/Status Sensors ===
    CuratedSensor(
        "window_heating_state", "Window heating", icon="mdi:car-defrost-rear"
    ),
    CuratedSensor("bem_level", "BEM level", None, None, None, icon="mdi:information"),
)

CURATED_BINARY_FLAT: tuple[CuratedBinary, ...] = (
    # === General Lock State ===
    CuratedBinary("locked", "Vehicle locked", "lock", invert=True, icon="mdi:car-key"),
    # === Individual Door Lock States (value 2=locked, 3=unlocked) ===
    CuratedBinary(
        "locked_state_front_left_door",
        "Front left door lock",
        "lock",
        invert=True,
        icon="mdi:car-door-lock",
    ),
    CuratedBinary(
        "locked_state_front_right_door",
        "Front right door lock",
        "lock",
        invert=True,
        icon="mdi:car-door-lock",
    ),
    CuratedBinary(
        "locked_state__rear_left_door",
        "Rear left door lock",
        "lock",
        invert=True,
        icon="mdi:car-door-lock",
    ),
    CuratedBinary(
        "locked_state_rear_right_door",
        "Rear right door lock",
        "lock",
        invert=True,
        icon="mdi:car-door-lock",
    ),
    CuratedBinary(
        "locked_state_tailgate",
        "Tailgate lock",
        "lock",
        invert=True,
        icon="mdi:car-door-lock",
    ),
    CuratedBinary(
        "locked_state_front_engine_bonnet",
        "Hood lock",
        "lock",
        invert=True,
        icon="mdi:car-door-lock",
    ),
    # === Door Open States (value 2=open, 3=closed, 0=unsupported, 1=invalid) ===
    CuratedBinary(
        "open_state_front_left_door", "Front left door", "door", icon="mdi:car-door"
    ),
    CuratedBinary(
        "open_state_front_right_door", "Front right door", "door", icon="mdi:car-door"
    ),
    CuratedBinary(
        "open_state_rear_left_door", "Rear left door", "door", icon="mdi:car-door"
    ),
    CuratedBinary(
        "open_state_rear_right_door", "Rear right door", "door", icon="mdi:car-door"
    ),
    CuratedBinary("open_state_tailgate", "Tailgate", "door", icon="mdi:car-back"),
    CuratedBinary("open_state_front_engine_bonnet", "Hood", "door", icon="mdi:car"),
    # === Door Safe States (value 2=safe, 3=unsafe, 0=unsupported, 1=invalid) ===
    CuratedBinary(
        "safe_state_front_right_door",
        "Front right door safe",
        "safety",
        invert=True,
        icon="mdi:shield-car",
    ),
    CuratedBinary(
        "safe_state_rear_left_door",
        "Rear left door safe",
        "safety",
        invert=True,
        icon="mdi:shield-car",
    ),
    CuratedBinary(
        "safe_state_rear_right_door",
        "Rear right door safe",
        "safety",
        invert=True,
        icon="mdi:shield-car",
    ),
    CuratedBinary(
        "safe_state_tailgate",
        "Tailgate safe",
        "safety",
        invert=True,
        icon="mdi:shield-car",
    ),
    CuratedBinary(
        "safe_state_front_engine_bonnet",
        "Hood safe",
        "safety",
        invert=True,
        icon="mdi:shield-car",
    ),
    # === Window States (value 2=open, 3=closed, 0=unsupported, 1=invalid) ===
    CuratedBinary(
        "state_front_left_door_window_lifter",
        "Front left window",
        "window",
        icon="mdi:window-open-variant",
    ),
    CuratedBinary(
        "state_front_right_door_window_lifter",
        "Front right window",
        "window",
        icon="mdi:window-open-variant",
    ),
    CuratedBinary(
        "state_rear_left_door_window_lifter",
        "Rear left window",
        "window",
        icon="mdi:window-open-variant",
    ),
    CuratedBinary(
        "state_rear_right_door_window_lifter",
        "Rear right window",
        "window",
        icon="mdi:window-open-variant",
    ),
    # === Sunroof States ===
    CuratedBinary(
        "state_sunroof_motor_hood_1", "Sunroof", "window", icon="mdi:car-convertible"
    ),
    CuratedBinary(
        "state_sunroof_motor_hood_3",
        "Sunroof motor 3",
        None,
        icon="mdi:car-convertible",
    ),
    # === Other Binary States ===
    CuratedBinary(
        "parking_brake",
        "Parking brake",
        None,
        icon="mdi:car-brake-parking",
        encoding="onoff",
    ),
    CuratedBinary(
        "parking_lights",
        "Parking lights",
        "light",
        icon="mdi:car-parking-lights",
        encoding="lights",
    ),
    CuratedBinary("state_of_hood", "Hood state", None, icon="mdi:car"),
    CuratedBinary("state_service_hatch", "Service hatch", None, icon="mdi:gas-station"),
    CuratedBinary("state_spoiler", "Spoiler", None, icon="mdi:car-sports"),
)

# ---------------------------------------------------------------------------
# Combined fields for backward compatibility and field validation
# ---------------------------------------------------------------------------

CURATED_FIELDS: frozenset[str] = frozenset(
    [s.field_name for s in CURATED_SENSORS_DOTTED]
    + [s.field_name for s in CURATED_SENSORS_FLAT]
    + [b.field_name for b in CURATED_BINARY_DOTTED]
    + [b.field_name for b in CURATED_BINARY_FLAT]
)
