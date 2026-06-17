# VW Group EU Data Act — Home Assistant integration

Periodically downloads your vehicle's "continuous data" from the Volkswagen
Group EU Data Act portal (`eu-data-act.drivesomethinggreater.com`) and exposes
it in Home Assistant.

**Supported brands:** Volkswagen · Audi · Škoda · SEAT · CUPRA

## Features

- **Multi-brand support**: select your brand (Volkswagen, Audi, Škoda, SEAT or
  CUPRA) during setup — each brand uses its own OIDC identity flow.
- **Login with your brand credentials** and pick a VIN during setup (the portal
  is queried for the vehicles on your account).
- **Curated sensors** for the useful data points (battery SoC, target charge
  level, charge power, mileage, climate temperatures, charge state, doors
  locked, parking brake, …) — enabled by default with proper units and device
  classes.
- **Every other data point** is exposed as a *disabled-by-default* diagnostic
  sensor, enriched (name / unit / description) from the official PDF data
  dictionary. Enable the ones you want from the entity settings.
- **Adaptive polling**: the portal drops a new dataset roughly every 15 minutes.
  The integration refreshes shortly after each expected drop; if nothing new is
  available it retries once a minute until the next dataset appears, then
  resumes the 15-minute cadence.
- **History** is provided by Home Assistant's normal recorder: numeric sensors
  with a state class accrue long-term statistics from their live values going
  forward (the integration does not back-fill past datapoints — see notes).

## Prerequisites — enable continuous data on the portal first

Before adding the integration, you must enable a **continuous 15-minute data
request** for your vehicle on the EU Data Act portal. The integration only
*downloads* the datasets the portal generates — it cannot create the data
request for you, and without an active request there will be nothing to fetch.

1. Open <https://eu-data-act.drivesomethinggreater.com/> and **log in** with
   your brand ID (Volkswagen ID / myAudi / myŠkoda / SEAT / CUPRA — the same
   email/password you'll use in Home Assistant).
2. Go to **Data clusters → Vehicle overview**.
3. **Connect your car** to the site if it isn't already listed (follow the
   on-screen pairing/consent steps for your VIN).
4. Click **Get customised data** for the vehicle and follow the instructions to
   configure a **continuous** data request with a **15-minute** frequency.
5. Wait until the portal starts producing datasets (you'll see ZIP files appear
   in the vehicle's data delivery list, roughly every 15 minutes). The first
   file can take up to a few hours to show up.

Once datasets are being generated, continue with the installation below.

> The integration polls at most every 15 minutes because that is how often the
> portal publishes new data — a shorter interval cannot produce fresher values.

## Installation

### Option A — HACS (recommended)

[HACS](https://hacs.xyz) must already be installed in Home Assistant.

1. In Home Assistant go to **HACS** (sidebar).
2. Open the **⋮** menu (top-right) → **Custom repositories**.
3. Add this repository:
   - **Repository:** `https://github.com/mikrohard/hass-vw-eu-data-act`
   - **Type / Category:** **Integration**

   Then click **Add**.
4. Back in HACS, search for **VW Group EU Data Act**, open it, and click
   **Download** (pick the latest version).
5. **Restart Home Assistant** when prompted.
6. Continue with [Add the integration](#add-the-integration) below.

> Once the repository is published/approved you can instead use this one-click
> link (replace with your published URL):
> *HACS → Integrations → Explore & Download → "VW Group EU Data Act"*.

### Option B — Manual

1. Copy the `custom_components/vw_eu_data_act` folder into your Home Assistant
   `config/custom_components/` directory (so you end up with
   `config/custom_components/vw_eu_data_act/manifest.json`).
2. Restart Home Assistant.

### Add the integration

1. *Settings → Devices & Services → **Add Integration** → search "VW Group EU
   Data Act"*.
2. **Select your brand** (Volkswagen, Audi, Škoda, SEAT or CUPRA).
3. Enter the **same email/password** you used on the portal, then select your
   vehicle from the list.

## Notes & limitations

- **No historical back-fill.** The integration only records values from the
  moment it's running (Home Assistant's recorder builds history/statistics from
  the live sensor states). It does **not** import the portal's last ~30 datasets
  into long-term statistics — doing so collided with the recorder's own
  statistics for the same entities and could corrupt unrelated statistics.
- Datasets named `*_no_content_found.zip` are skipped (the vehicle produced no
  payload for that interval).
- Credentials are stored in the Home Assistant config entry and used only to
  authenticate against the official portal.

## Troubleshooting the login

If setup fails to accept your credentials, you can reproduce and debug the
login flow **outside Home Assistant** with the bundled tester:

```bash
python3 -m venv .venv && .venv/bin/pip install aiohttp
# full login + vehicle/dataset listing:
EUDA_EMAIL='you@example.com' EUDA_PASSWORD='secret' .venv/bin/python tools/test_login.py
# or just inspect the public sign-in page structure (no password sent):
.venv/bin/python tools/test_login.py --dump you@example.com x
```

It prints DEBUG-level progress for each login step (priming → authorize →
identifier POST → password POST → portal callback) so you can see exactly where
it stops. To get the same detail from inside Home Assistant, add:

```yaml
logger:
  logs:
    custom_components.vw_eu_data_act: debug
```

> The portal's `/services/redirect/authentication` endpoint returns HTTP 500 for
> non-browser clients, so the integration builds the OIDC `authorize` URL
> directly. If login fails, make sure you selected the **correct brand** in the
> setup flow — each VW Group brand uses a different OIDC client ID and identity
> flow.

## Updating the data dictionary

`custom_components/vw_eu_data_act/data_dictionary.json` is generated from the
official PDF and committed to the repo. To regenerate from a newer PDF:

```bash
python -m venv .venv && .venv/bin/pip install pdfplumber
.venv/bin/python tools/parse_dictionary.py path/to/DataDictionary.pdf
```

## License

Released under the [MIT License](LICENSE).
