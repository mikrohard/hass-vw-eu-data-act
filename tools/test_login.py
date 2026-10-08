#!/usr/bin/env python3
"""Standalone tester for the EU Data Act login flow (no Home Assistant needed).

Runs the *real* EudaApiClient login against the live portal so you can debug
authentication outside Home Assistant.

Setup (once):
    python3 -m venv .venv
    .venv/bin/pip install aiohttp

Run:
    EUDA_EMAIL='you@example.com' EUDA_PASSWORD='secret' \
        .venv/bin/python tools/test_login.py

    # or pass on the command line:
    .venv/bin/python tools/test_login.py you@example.com 'secret'

    # full diagnostic: dump every page's HTML + forms to /tmp and walk the flow
    .venv/bin/python tools/test_login.py --dump you@example.com 'secret'

The --dump mode never prints your password and writes the captured HTML to
/tmp/euda_*.html so you can inspect the real page structure.
"""
from __future__ import annotations

import asyncio
import getpass
import importlib.util
import logging
import os
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKG_DIR = ROOT / "custom_components" / "vw_eu_data_act"
PKG = "vw_eu_data_act"


def _load():
    """Load const/data/api without importing Home Assistant."""
    try:
        import aiohttp  # noqa: F401
    except ModuleNotFoundError:
        print("ERROR: aiohttp is not installed. Run: .venv/bin/pip install aiohttp")
        sys.exit(2)
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


async def run_normal(api, email: str, password: str) -> int:
    import aiohttp

    session = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar())
    client = api.EudaApiClient(session, email, password)
    try:
        print("\n=== Logging in ===")
        await client.async_login()
        print("LOGIN OK\n")

        print("=== Vehicles ===")
        vehicles = await client.async_list_vehicles()
        for v in vehicles:
            print(f"  {v.get('vin')}  nickname={v.get('nickname')}")
        if not vehicles:
            print("  (no vehicles returned - check the raw vehicles response)")
            return 1

        vin = vehicles[0]["vin"]
        print(f"\n=== Metadata for {vin} ===")
        meta = await client.async_get_metadata(vin)
        identifier = meta.get("Identifier")
        print(f"  Identifier={identifier}  Frequency={meta.get('Frequency')}")

        if identifier:
            print(f"\n=== Dataset list for {vin} ===")
            datasets = await client.async_list_datasets(vin, identifier)
            print(f"  {len(datasets)} datasets")
            for d in datasets[:3]:
                print(f"    {d.get('name')}  createdOn={d.get('createdOn')}")
            if datasets:
                newest = datasets[0]["name"]
                print(f"\n=== Downloading {newest} ===")
                payload = await client.async_download_dataset(vin, identifier, newest)
                print(f"  parsed JSON: vin={payload.get('vin')} points={len(payload.get('Data', []))}")
        print("\nALL OK")
        return 0
    except Exception as err:  # noqa: BLE001
        print(f"\nFAILED: {type(err).__name__}: {err}")
        return 1
    finally:
        await session.close()


async def run_dump(api, mods, email: str, password: str) -> int:
    """Walk the flow manually, saving each page and listing all forms."""
    import re

    import aiohttp
    from html.parser import HTMLParser

    const = mods["const"]

    class AllForms(HTMLParser):
        def __init__(self):
            super().__init__()
            self.forms = []
            self._cur = None

        def handle_starttag(self, tag, attrs):
            a = dict(attrs)
            if tag == "form":
                self._cur = {"action": a.get("action"), "fields": {}}
                self.forms.append(self._cur)
            elif tag == "input" and self._cur is not None:
                if a.get("name"):
                    self._cur["fields"][a["name"]] = a.get("value") or ""

        def handle_endtag(self, tag):
            if tag == "form":
                self._cur = None

    def dump(name, url, html):
        path = Path("/tmp") / f"euda_{name}.html"
        path.write_text(html, encoding="utf-8")
        p = AllForms()
        p.feed(html)
        print(f"\n--- {name}: {url}")
        print(f"    saved {len(html)} bytes -> {path}")
        for i, f in enumerate(p.forms):
            print(f"    form[{i}] action={f['action']} fields={sorted(f['fields'])}")
        if not p.forms:
            print("    (no <form> tags found - page may be JS-rendered)")

    session = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar())
    client = api.EudaApiClient(session, email, password)
    try:
        from urllib.parse import urljoin

        authorize_url = client._build_authorize_url()
        print(f"\nauthorize_url = {authorize_url}")
        async with await client._get(authorize_url) as resp:
            signin_url, signin_html = str(resp.url), await resp.text()
        dump("1_signin", signin_url, signin_html)

        # POST the email only (no password) to reach the password page.
        fields, action = api._login_fields(signin_html)
        print(f"\nstep2 extracted: action={action} fields={sorted(fields)}")
        fields["email"] = email
        async with session.post(
            urljoin(signin_url, action or ""), data=fields, headers={"User-Agent": api.USER_AGENT}
        ) as resp:
            auth_url, auth_html = str(resp.url), await resp.text()
        dump("2_authenticate", auth_url, auth_html)

        fields2, action2 = api._login_fields(auth_html)
        print(f"\nstep3 extracted: action={action2} fields={sorted(fields2)}")
        has = "hmac" in fields2 and "_csrf" in fields2
        print(f"step3 has hmac+_csrf needed for password POST: {has}")
        err = api._login_error(auth_html)
        if err:
            print(f"step3 page reports error: {err!r}")
        print("\nCookies so far:", [c.key for c in session.cookie_jar])
        print(
            "\nNo password was sent. If 'has hmac+_csrf' is True above, run without "
            "--dump to attempt the full login."
        )
        return 0
    finally:
        await session.close()


def main() -> int:
    logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(name)s: %(message)s")
    mods = _load()
    api = mods["api"]

    argv = [a for a in sys.argv[1:] if a != "--dump"]
    dump_mode = "--dump" in sys.argv

    email = (argv[0] if len(argv) > 0 else os.environ.get("EUDA_EMAIL")) or input("Email: ")
    password = (
        argv[1] if len(argv) > 1 else os.environ.get("EUDA_PASSWORD")
    ) or getpass.getpass("Password: ")

    runner = run_dump(api, mods, email, password) if dump_mode else run_normal(api, email, password)
    return asyncio.run(runner)


if __name__ == "__main__":
    raise SystemExit(main())
