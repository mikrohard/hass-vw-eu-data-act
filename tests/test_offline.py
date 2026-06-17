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
