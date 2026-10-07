"""Offline tests for the HA-independent core (data.py + api.py helpers).

Loads the integration's pure modules without importing Home Assistant by
constructing a minimal `vw_eu_data_act` package namespace and loading the
submodules that have no HA dependency.
"""
from __future__ import annotations

import importlib.util
import io
import json
import sys
import types
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKG_DIR = ROOT / "custom_components" / "vw_eu_data_act"
PKG = "vw_eu_data_act"


def _load():
    pkg = types.ModuleType(PKG)
    pkg.__path__ = [str(PKG_DIR)]
    sys.modules[PKG] = pkg
    mods = {}
    for name in ("const", "data", "api"):
        spec = importlib.util.spec_from_file_location(f"{PKG}.{name}", PKG_DIR / f"{name}.py")
        mod = importlib.util.module_from_spec(spec)
        mod.__package__ = PKG
        sys.modules[f"{PKG}.{name}"] = mod
        spec.loader.exec_module(mod)
        mods[name] = mod
    return mods


def main() -> int:
    mods = _load()
    const = mods["const"]
    data = mods["data"]
    api = mods["api"]
    failures: list[str] = []

    def check(label, got, want):
        ok = got == want
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}: {got!r}" + ("" if ok else f" (want {want!r})"))
        if not ok:
            failures.append(label)

    # --- value parsing ----------------------------------------------------
    print("value parsing:")
    check("int", data.parse_value("116803", "int"), 116803)
    check("float", data.parse_value("0.0", "float"), 0.0)
    check("bool true", data.parse_value("true", "boolean"), True)
    check("bool false", data.parse_value("false"), False)
    check("duration 0s", data.parse_value("0s"), 0.0)
    check("duration 1800s", data.parse_value("1800s"), 1800.0)
    check("enum stays str", data.parse_value("WINDOW_HEATING_STATE_OFF"), "WINDOW_HEATING_STATE_OFF")
    check("empty -> None", data.parse_value(""), None)

    # --- dictionary -------------------------------------------------------
    print("data dictionary:")
    dd = data.load_dictionary()
    check("dict non-empty", len(dd) > 1000, True)
    check(
        "remaining_climate_time name",
        dd.get("3c19831c-38b8-3dc5-9ead-bb333616d925", {}).get("name"),
        "remaining_climate_time",
    )

    # --- dataset (real sample if present, else a synthetic one) ----------
    print("sample dataset:")
    sample_path = ROOT / "WVWZZZTESTVIN0001_20260530052434.json"
    if sample_path.exists():
        sample = json.loads(sample_path.read_text())
    else:
        print("  (sample JSON absent - using synthetic dataset, no personal data)")
        sample = {
            "vin": "WVWZZZTESTVIN0001",
            "user_id": "test",
            "Data": [
                {"key": "k1", "dataFieldName": "battery_state_report.soc", "value": "69"},
                {"key": "k2", "dataFieldName": "mileage.value", "value": "116803"},
                {"key": "k3", "dataFieldName": "settings.target_soc", "value": "80"},
                {"key": "k4", "dataFieldName": "battery_state_report.charge_power", "value": "0.0"},
                {"key": "k5", "dataFieldName": "min_temperature", "value": "19.5"},
                {"key": "k6", "dataFieldName": "locked", "value": "true"},
                {"key": "k7", "dataFieldName": "parking_brake", "value": "true"},
                {"key": "k8", "dataFieldName": "remaining_climate_time", "value": "0s"},
                {"key": "k9", "dataFieldName": "car_captured_time", "value": "2026-05-29T22:59:27Z"},
                {"key": "k10", "dataFieldName": "report_type", "value": "RPT_0"},
            ],
        }
    ds = data.Dataset.from_json(sample)
    check("vin", ds.vin, "WVWZZZTESTVIN0001")
    check("soc", _field_val(ds, "battery_state_report.soc"), 69)
    check("mileage", _field_val(ds, "mileage.value"), 116803)
    check("target_soc", _field_val(ds, "settings.target_soc"), 80)
    check("charge_power", _field_val(ds, "battery_state_report.charge_power"), 0.0)
    check("min_temperature", _field_val(ds, "min_temperature"), 19.5)
    check("locked", _field_val(ds, "locked"), True)
    check("parking_brake", _field_val(ds, "parking_brake"), True)
    check("remaining_climate_time", _field_val(ds, "remaining_climate_time"), 0.0)
    check("captured_at present", ds.captured_at is not None, True)

    # --- captured_at: latest across duplicate car_captured_time entries ---
    print("captured_at max:")
    ds_cap = data.Dataset.from_json(
        {
            "vin": "V",
            "user_id": "u",
            "Data": [
                {
                    "key": "old",
                    "dataFieldName": "car_captured_time",
                    "value": "2026-06-10T09:16:00+00:00",
                },
                {
                    "key": "new",
                    "dataFieldName": "car_captured_time",
                    "value": "2026-06-17T09:52:47+00:00",
                },
            ],
        }
    )
    check(
        "captured_at is latest",
        ds_cap.captured_at,
        datetime(2026, 6, 17, 9, 52, 47, tzinfo=timezone.utc),
    )

    # --- duplicate field: deterministic selection regardless of order -----
    print("duplicate field selection:")
    dup_entries = [
        {"key": "ccc", "dataFieldName": "charging_state_report.current_charge_state", "value": "C"},
        {"key": "aaa", "dataFieldName": "charging_state_report.current_charge_state", "value": "A"},
        {"key": "bbb", "dataFieldName": "charging_state_report.current_charge_state", "value": "B"},
    ]
    picks = set()
    for order in ([0, 1, 2], [2, 1, 0], [1, 2, 0]):
        ds_d = data.Dataset.from_json(
            {"vin": "V", "user_id": "u", "Data": [dup_entries[i] for i in order]}
        )
        picks.add(_field_val(ds_d, "charging_state_report.current_charge_state"))
    # always the smallest-key entry ("aaa" -> "A"), independent of array order
    check("stable pick under shuffle", picks, {"A"})

    # --- curated / raw classification ------------------------------------
    print("curated registry:")
    check("soc is curated", "battery_state_report.soc" in data.CURATED_FIELDS, True)
    check("locked is curated", "locked" in data.CURATED_FIELDS, True)
    _mintemp = next(s for s in data.CURATED_SENSORS_FLAT if s.field_name == "min_temperature")
    check("min_temperature named battery", _mintemp.name, "Battery min temperature")

    # --- binary state decoding (encoding-driven, not field-name guessing) -
    print("binary decode:")
    dec = data.decode_binary_state
    # plain booleans pass through; invert flips
    check("bool true", dec(True, "open", False), True)
    check("bool invert", dec(True, "open", True), False)
    # "open": 2=active(on), 3=inactive(off), 0/1=unknown
    check("open 2 -> on", dec(2, "open", False), True)
    check("open 3 -> off", dec(3, "open", False), False)
    check("open 0 -> unknown", dec(0, "open", False), None)
    check("open 1 -> unknown", dec(1, "open", False), None)
    # lock/safe reuse "open" with invert: 2=locked -> off, 3=unlocked -> on
    check("lock 2 (locked) -> off", dec(2, "open", True), False)
    check("lock 3 (unlocked) -> on", dec(3, "open", True), True)
    # "onoff": parking_brake 0=off, 1=on
    check("onoff 0 -> off", dec(0, "onoff", False), False)
    check("onoff 1 -> on", dec(1, "onoff", False), True)
    # "lights": 0/1=unknown, 2=off, 3/4/5=on
    check("lights 1 -> unknown", dec(1, "lights", False), None)
    check("lights 2 -> off", dec(2, "lights", False), False)
    check("lights 4 -> on", dec(4, "lights", False), True)
    # missing value stays unknown
    check("none -> unknown", dec(None, "open", False), None)
    # "enum": string labels mapped through on_values / off_values
    _on = ("BCAM_ACTIVATION_ACTIVATED",)
    _off = ("BCAM_ACTIVATION_DEACTIVATED",)
    check("enum on", dec("BCAM_ACTIVATION_ACTIVATED", "enum", False, _on, _off), True)
    check("enum off", dec("BCAM_ACTIVATION_DEACTIVATED", "enum", False, _on, _off), False)
    check("enum invalid -> unknown", dec("BCAM_ACTIVATION_INVALID", "enum", False, _on, _off), None)
    check("enum invert", dec("BCAM_ACTIVATION_ACTIVATED", "enum", True, _on, _off), False)
    _bcam = next(b for b in data.CURATED_BINARY_DOTTED if b.field_name == "setting.bcam_activation")
    check("bcam encoding", _bcam.encoding, "enum")
    check("bcam on_values", _bcam.on_values, _on)

    # --- ID.3 curated additions: energy content / timers / consumption -----
    print("ID.3 curated additions:")
    check("tenths 483.5 -> 48.35", data.tenths_to_units(483.5), 48.35)
    check("tenths str", data.tenths_to_units("237.5"), 23.75)
    check("tenths int", data.tenths_to_units(10), 1.0)
    check("tenths None", data.tenths_to_units(None), None)
    check("tenths bool -> None", data.tenths_to_units(True), None)
    check("tenths junk -> None", data.tenths_to_units("n/a"), None)
    _ts = data.parse_timestamp("2026-09-17T22:24:00Z")
    check("parse_timestamp iso Z", _ts, datetime(2026, 9, 17, 22, 24, tzinfo=timezone.utc))
    check("parse_timestamp tz-aware", _ts.tzinfo is not None, True)
    check("parse_timestamp None", data.parse_timestamp(None), None)
    check("parse_timestamp junk", data.parse_timestamp("soon"), None)
    _dotted = {c.field_name: c for c in data.CURATED_SENSORS_DOTTED}
    for _f in (
        "energy_contents.current_energy_content.physical_value",
        "energy_contents.maximal_energy_content.physical_value",
        "profile_state_report.next_charging_timer_information.estimated_start_time",
        "profile_state_report.next_charging_timer_information.estimated_finish_time",
        "profile_state_report.next_charging_timer_information.target_reachability",
        "charging_state_report.profile_charge_reason",
        "settings.auto_unlock_ac",
        "additional_consumptions.interior_climatization_consumption",
        "additional_consumptions.residual_consumption",
        "update_reason",
    ):
        check(f"{_f} curated", _f in _dotted, True)
    check("energy content transform", _dotted["energy_contents.current_energy_content.physical_value"].transform, "tenths")
    check("energy content unit", _dotted["energy_contents.maximal_energy_content.physical_value"].unit, "kWh")
    check("climate consumption transform", _dotted["additional_consumptions.interior_climatization_consumption"].transform, "tenths")
    check("residual consumption transform", _dotted["additional_consumptions.residual_consumption"].transform, "tenths")
    check("timer transform", _dotted["profile_state_report.next_charging_timer_information.estimated_finish_time"].transform, "timestamp")
    check("timer device class", _dotted["profile_state_report.next_charging_timer_information.estimated_finish_time"].device_class, "timestamp")
    check("update_reason diagnostic", _dotted["update_reason"].diagnostic, True)
    check("soc not diagnostic", _dotted["battery_state_report.soc"].diagnostic, False)
    # slope consumption deliberately left raw (unit / meaning undocumented)
    check("slope stays raw", "slope_consumption_values.ascent_slope_consumption.physical_value" in data.CURATED_FIELDS, False)
    # registry wires the special encodings to the right fields
    _pbrake = next(b for b in data.CURATED_BINARY_FLAT if b.field_name == "parking_brake")
    check("parking_brake encoding", _pbrake.encoding, "onoff")
    _plights = next(b for b in data.CURATED_BINARY_FLAT if b.field_name == "parking_lights")
    check("parking_lights encoding", _plights.encoding, "lights")
    _door = next(b for b in data.CURATED_BINARY_FLAT if b.field_name == "open_state_tailgate")
    check("door default encoding", _door.encoding, "open")

    # --- raw unique_id namespaced by VIN (multi-vehicle, issue #7) --------
    print("raw unique_id namespacing:")
    key = "1763a4fe-d8a6-3b8c-b095-70081f3e61c7"  # a key shared across vehicles
    check("vin-prefixed", const.raw_unique_id("VINA", key), f"VINA_{key}")
    check("distinct per vehicle", const.raw_unique_id("VINA", key) != const.raw_unique_id("VINB", key), True)

    # --- sticky values: keep last when an update omits a field (issue #9) -
    print("sticky values:")
    check("fresh value kept", data.sticky(50, 55), 55)
    check("missing -> previous retained", data.sticky(55, None), 55)
    check("zero is not missing", data.sticky(55, 0), 0)
    check("false is not missing", data.sticky(True, False), False)
    present = {dp.field_name for dp in ds.points.values()}
    curated_present = present & data.CURATED_FIELDS
    raw_count = len(ds.points) - sum(
        1 for dp in ds.points.values() if dp.field_name in data.CURATED_FIELDS
    )
    print(f"    points={len(ds.points)} curated_present={len(curated_present)} raw={raw_count}")
    check("some curated present", len(curated_present) >= 5, True)

    # --- api zip helper ---------------------------------------------------
    print("api helpers:")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("WVWZZZTESTVIN0001_x.json", json.dumps(sample))
    parsed = api.EudaApiClient._unzip_json(buf.getvalue(), "x.zip")
    check("unzip vin", parsed["vin"], "WVWZZZTESTVIN0001")
    vins = api._extract_vins({"vehicles": [{"vin": "WVWZZZTESTVIN0001", "vehicleNickname": "ID.3"}]})
    check("extract_vins", vins, [{"vin": "WVWZZZTESTVIN0001", "nickname": "ID.3"}])

    # --- login field extraction (templateModel + html inputs) ------------
    print("login field extraction:")
    auth_page = (
        "<html><script>window._IDK = { templateModel: "
        '{"relayState":"RS","hmac":"HM","postAction":"login/authenticate",'
        '"error":null,"emailPasswordForm":{"email":"a@b.c"}}, '
        "csrf_token: 'CSRF1' }</script></html>"
    )
    f2, _ = api._login_fields(auth_page)
    check("templateModel hmac", f2.get("hmac"), "HM")
    check("templateModel _csrf", f2.get("_csrf"), "CSRF1")
    check("templateModel relayState", f2.get("relayState"), "RS")
    err_page = auth_page.replace('"error":null', '"error":{"text":"Bad creds"}')
    check("login error text", api._login_error(err_page), "Bad creds")
    email_page = (
        '<form action="/x/login/identifier"><input name=_csrf value=HC>'
        "<input name=hmac value=HH><input name=relayState value=RS><input name=email></form>"
    )
    fe, ae = api._login_fields(email_page)
    check("html-input _csrf not overridden", fe.get("_csrf"), "HC")
    check("html-input action", ae, "/x/login/identifier")

    # --- distance unit resolved from companion *.unit field --------------
    print("distance unit resolution:")
    check("MILES -> mi", data.resolve_distance_unit("MILES"), "mi")
    check("KM -> km", data.resolve_distance_unit("KM"), "km")
    check("lowercase miles -> mi", data.resolve_distance_unit("miles"), "mi")
    check("unknown -> None", data.resolve_distance_unit("LIGHTYEARS"), None)
    mileage = next(s for s in data.CURATED_SENSORS_DOTTED if s.field_name == "mileage.value")
    check("mileage declares unit_field", mileage.unit_field, "mileage.unit")
    # a miles dataset exposes mileage.unit so the sensor can pick "mi"
    ds_mi = data.Dataset.from_json({"vin": "V", "user_id": "u", "Data": [
        {"key": "m1", "dataFieldName": "mileage.value", "value": "43531"},
        {"key": "m2", "dataFieldName": "mileage.unit", "value": "MILES"},
    ]})
    unit_dp = ds_mi.by_field("mileage.unit")
    check("resolved unit from dataset", data.resolve_distance_unit(unit_dp.value), "mi")

    # --- (5) friendly names for bare fields ------------------------------
    print("friendly raw names:")
    check("bare value -> description", data.friendly_name("value", "Value of the primary range"), "Value of the primary range")
    check("dotted name kept", data.friendly_name("battery_state_report.soc", "State of charge"), "battery_state_report.soc")
    check("bare value no desc -> value", data.friendly_name("value", None), "value")

    # --- (6) enum integer fallback resolves to label --------------------
    print("enum integer fallback:")
    enum_desc = (
        "IMMEDIATE_ACTION_STAT E_INVALID, IMMEDIATE_ACTION_STAT E_IMMEDIATE_ACTION_TI ME, "
        "IMMEDIATE_ACTION_STAT E_IMMEDIATE_CHARGING , IMMEDIATE_ACTION_STAT E_IMMEDIATE_ACTION_ST OPPED, "
        "IMMEDIATE_ACTION_STAT E_IMMEDIATE_ACTION_R ANGE, IMMEDIATE_ACTION_STAT E_IMMEDIATE_ACTION_S OC, "
        "IMMEDIATE_ACTION_STAT E_CHARGE_MODE_SELEC TION"
    )
    members = data.enum_members(enum_desc)
    check("parses 7 enum members", len(members), 7)
    dp_int = data.DataPoint("k", "charging_state_report.immediate_action_state", "6", "enum", None, enum_desc)
    check("int 6 -> label", dp_int.value, "IMMEDIATE_ACTION_STATE_CHARGE_MODE_SELECTION")
    dp_str = data.DataPoint("k", "f", "IMMEDIATE_ACTION_STATE_IMMEDIATE_CHARGING", "enum", None, enum_desc)
    check("string label unchanged", dp_str.value, "IMMEDIATE_ACTION_STATE_IMMEDIATE_CHARGING")
    dp_prose = data.DataPoint("k", "report_type", "3", "enum", None, "The enum value of report type")
    check("prose enum desc -> int kept", dp_prose.value, 3)

    # --- protocol sentinels ----------------------------------------------
    # 0 = "unsupported", 1 = "invalid". A car without tyre-pressure sensors
    # reports 1 on every tyre field, and "1.0 bar" reads as a dangerously flat
    # tyre rather than as missing data.
    print("sentinel stripping:")
    check("0 -> None", data.strip_sentinel(0, (0, 1)), None)
    check("1 -> None", data.strip_sentinel(1, (0, 1)), None)
    check("real reading kept", data.strip_sentinel(2.4, (0, 1)), 2.4)
    check("no sentinels declared -> passthrough", data.strip_sentinel(1, ()), 1)
    check("None stays None", data.strip_sentinel(None, (0, 1)), None)
    check("non-numeric passthrough", data.strip_sentinel("OFF", (0, 1)), "OFF")
    check("bool untouched", data.strip_sentinel(True, (0, 1)), True)

    tyre = [c for c in data.CURATED_SENSORS_FLAT if c.field_name.startswith("tyre_pressure")]
    check("all tyre sensors declare sentinels", all(c.sentinels == (0, 1) for c in tyre), True)
    check("tyre sensors covered", len(tyre) >= 10, True)

    # --- service intervals -------------------------------------------------
    # The portal counts down through negative numbers and crosses zero when the
    # service becomes overdue, so the sign carries the meaning.
    print("service intervals:")
    check("26200 km remaining", data.service_interval_remaining(-26200), 26200)
    check("270 days remaining", data.service_interval_remaining("-270"), 270)
    check("overdue stays negative", data.service_interval_remaining(500), -500)
    check("at the limit", data.service_interval_remaining(0), 0)
    check("non-numeric -> None", data.service_interval_remaining("n/a"), None)
    check("None -> None", data.service_interval_remaining(None), None)
    check(
        "overdue distinguishable from remaining",
        data.service_interval_remaining(500) != data.service_interval_remaining(-500),
        True,
    )

    maint = [
        c
        for c in data.CURATED_SENSORS_FLAT
        if c.field_name.startswith("maintenance_interval")
    ]
    check("maintenance sensors use service_interval",
          all(c.transform == "service_interval" for c in maint), True)
    check("maintenance sensors covered", len(maint), 4)

    # --- entities with nothing to report ----------------------------------
    # A field being listed in the dataset does not mean the vehicle has the
    # hardware; without this gate a car with no TPMS/sunroof/spoiler gets
    # permanently empty entities.
    print("empty-entity suppression:")
    tyre_sensor = next(
        c for c in data.CURATED_SENSORS_FLAT
        if c.field_name == "tyre_pressure_actual_front_left"
    )
    plain = next(c for c in data.CURATED_SENSORS_FLAT if c.field_name == "mileage")

    def _dp(field, raw):
        return data.DataPoint(field, field, raw, "int")

    check(
        "sentinel reading -> no entity",
        data.curated_has_reading(_dp("tyre_pressure_actual_front_left", "1"), tyre_sensor),
        False,
    )
    check(
        "real reading -> entity",
        data.curated_has_reading(_dp("tyre_pressure_actual_front_left", "24"), tyre_sensor),
        True,
    )
    check(
        "empty string -> no entity",
        data.curated_has_reading(_dp("mileage", ""), plain),
        False,
    )
    check("zero is a reading", data.curated_has_reading(_dp("mileage", "0"), plain), True)
    check(
        "unsentinelled 1 is a reading",
        data.curated_has_reading(_dp("mileage", "1"), plain),
        True,
    )
    # Binary equivalent: 0/1 are "unsupported"/"invalid" under the "open"
    # encoding, but real states under "onoff".
    check("binary sentinel -> None", data.decode_binary_state(1, "open"), None)
    check("parking brake 0 is a state", data.decode_binary_state(0, "onoff"), False)

    # Doors document safe (2) / unsafe (3) and report 2; the tailgate and
    # bonnet document no "safe" value and report a constant 3, so exposing
    # them as safety sensors pins them to "problem" with the bonnet shut.
    safe_fields = {
        c.field_name for c in data.CURATED_BINARY_FLAT if "safe_state" in c.field_name
    }
    check(
        "no safety sensor for tailgate/bonnet",
        safe_fields & {"safe_state_tailgate", "safe_state_front_engine_bonnet"},
        set(),
    )
    check("door safety sensors kept", len(safe_fields), 3)
    check(
        "a door reporting safe(2) is not a problem",
        data.decode_binary_state(2, "open", invert=True),
        False,
    )

    # --- raw sensor units --------------------------------------------------
    # The dictionary writes units inconsistently and some entries are prose or
    # a list of alternatives; only units the raw value already uses may be
    # attached, because raw sensors do no arithmetic.
    print("raw units:")
    check("(V) -> V", data.normalize_unit("(V)"), ("V", "voltage"))
    check("km -> km", data.normalize_unit("km"), ("km", "distance"))
    check("(km) -> km", data.normalize_unit("(km)"), ("km", "distance"))
    check("kmPerHour -> km/h", data.normalize_unit("kmPerHour"), ("km/h", "speed"))
    check("double-encoded degree repaired", data.normalize_unit("(Â°C)"), ("°C", "temperature"))
    check("prose unit dropped", data.normalize_unit("Hex (Interpreted)"), (None, None))
    check("ambiguous unit dropped", data.normalize_unit("10kPA / Bar / PSI/ kPA"), (None, None))
    check("scaled unit dropped", data.normalize_unit("kwH/1000km"), (None, None))
    check("deci-kelvin dropped", data.normalize_unit("dK"), (None, None))
    check("empty unit", data.normalize_unit(""), (None, None))

    dd = data.load_dictionary()
    check(
        "dictionary has no double-encoded degrees",
        any("Â°" in v.get("unit", "") for v in dd.values()),
        False,
    )

    # --- raw sensor names --------------------------------------------------
    print("raw names:")
    check(
        "description beats camelCase field",
        data.raw_entity_name("boardnetBatteryVoltageIndication", "current boardnet battery voltage"),
        "Current boardnet battery voltage",
    )
    check(
        "decimal point is not a sentence end",
        data.raw_entity_name("short_term_data_range_gain_distance",
                             "Gained range distance in [0.1 km] during short term trip"),
        "Gained range distance in [0.1 km] during short term trip",
    )
    check(
        "first sentence only",
        data.raw_entity_name("f", "Short label. Followed by more prose."),
        "Short label",
    )
    check(
        "value list is not a name",
        data.raw_entity_name("trueness", "fair, good, none, weak"),
        "Trueness",
    )
    check(
        "no description -> un-camel-cased field",
        data.raw_entity_name("boardnetBatteryVoltageIndication"),
        "Boardnet battery voltage indication",
    )
    check(
        "underscores become spaces",
        data.raw_entity_name("scope_potential_total"),
        "Scope potential total",
    )

    print()
    if failures:
        print(f"FAILED: {len(failures)} -> {failures}")
        return 1
    print("ALL OFFLINE TESTS PASSED")
    return 0


def _field_val(ds, field_name):
    dp = ds.by_field(field_name)
    return dp.value if dp else None


if __name__ == "__main__":
    raise SystemExit(main())
