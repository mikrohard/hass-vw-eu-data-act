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
