"""Constants for the VW EU Data Act integration."""
from __future__ import annotations

from datetime import timedelta

DOMAIN = "vw_eu_data_act"


def raw_unique_id(vin: str, key: str) -> str:
    """Unique_id for a raw data-point sensor.

    Dataset ``key`` UUIDs are shared across vehicles, so they must be namespaced
    by VIN to avoid collisions between config entries (one entry's entity would
    otherwise be dropped by the registry).
    """
    return f"{vin}_{key}"

# --- Portal / OIDC endpoints ---------------------------------------------
BASE_URL = "https://eu-data-act.drivesomethinggreater.com"
IDENTITY_BASE = "https://identity.vwgroup.io"

CALLBACK_LOGIN_PATH = "/services/callbacklogin"

# OIDC: we build the authorize URL directly instead of using the portal's
# /services/redirect/authentication servlet, which returns HTTP 500 for
# non-browser clients (it depends on AEM browser session state).
OIDC_AUTHORIZE_URL = IDENTITY_BASE + "/oidc/v1/authorize"
OIDC_SCOPE = "openid cars profile"
OIDC_REDIRECT_URI = BASE_URL + "/login"

# --- Multi-brand configuration --------------------------------------------
# Each brand has its own client_id and state string for the OIDC flow.
# Client IDs extracted from the evcc project (vehicle/vw/eudataact/types.go).

BRANDS: dict[str, dict[str, str]] = {
    "volkswagen": {
        "display_name": "Volkswagen",
        "client_id": "9b58543e-1c15-4193-91d5-8a14145bebb0@apps_vw-dilab_com",
        "state": "VOLKSWAGEN_PASSENGER_CARS",
    },
    "audi": {
        "display_name": "Audi",
        "client_id": "cc29b87a-5e9a-4362-aecf-5adea6b01bbb@apps_vw-dilab_com",
        "state": "AUDI",
    },
    "skoda": {
        "display_name": "Škoda",
        "client_id": "3ea88bf9-1d4e-4a68-b3ad-4098c1f1d246@apps_vw-dilab_com",
        "state": "SKODA",
    },
    # SEAT and CUPRA intentionally share one client_id (CUPRA runs on SEAT's
    # identity backend); they differ only by the state suffix. Matches evcc
    # (vehicle/vw/eudataact/types.go) — not a copy-paste slip.
    "seat": {
        "display_name": "SEAT",
        "client_id": "f85e5b69-e3b2-43aa-9c0d-1b7d0e0b576f@apps_vw-dilab_com",
        "state": "SEAT",
    },
    "cupra": {
        "display_name": "CUPRA",
        "client_id": "f85e5b69-e3b2-43aa-9c0d-1b7d0e0b576f@apps_vw-dilab_com",
        "state": "CUPRA",
    },
}

BRAND_CHOICES: dict[str, str] = {k: v["display_name"] for k, v in BRANDS.items()}

# Default brand for backward compatibility with existing config entries
DEFAULT_BRAND = "volkswagen"

# state encodes country__language__brand (echoed back to the portal callback).
DEFAULT_COUNTRY = "de"
DEFAULT_LANGUAGE = "en"

# Config entry key for brand selection
CONF_BRAND = "brand"


def get_oidc_client_id(brand: str = DEFAULT_BRAND) -> str:
    """Return the OIDC client_id for the given brand."""
    return BRANDS.get(brand, BRANDS[DEFAULT_BRAND])["client_id"]


def get_oidc_state(brand: str = DEFAULT_BRAND) -> str:
    """Return the OIDC state for the given brand."""
    brand_state = BRANDS.get(brand, BRANDS[DEFAULT_BRAND])["state"]
    return f"{DEFAULT_COUNTRY}__{DEFAULT_LANGUAGE}__{brand_state}"


# Legacy constants for backward compatibility (default to VW)
OIDC_CLIENT_ID = BRANDS[DEFAULT_BRAND]["client_id"]
OIDC_STATE = get_oidc_state(DEFAULT_BRAND)

# proxy_api paths (relative to BASE_URL)
VEHICLES_PATH = "/proxy_api/consent/me/vehicles"
RELATION_PATH = "/proxy_api/vum/v2/users/me/relations/{vin}"
METADATA_PATH = "/proxy_api/euda-apim/datarequest/vehicles/{vin}/metadata/partial"
LIST_PATH = "/proxy_api/euda-apim/datadelivery/vehicles/{vin}/{identifier}/list"
DOWNLOAD_PATH = "/proxy_api/euda-apim/datadelivery/vehicles/{vin}/{identifier}/download"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)

# --- Config entry keys ----------------------------------------------------
CONF_EMAIL = "email"
CONF_PASSWORD = "password"
CONF_VIN = "vin"
CONF_IDENTIFIER = "identifier"
CONF_NICKNAME = "nickname"

# --- Scheduling -----------------------------------------------------------
# Datasets land ~every 15 min; refresh shortly after the next expected drop.
DATASET_INTERVAL = timedelta(minutes=15)
POST_DATASET_BUFFER = timedelta(seconds=45)
RETRY_INTERVAL = timedelta(minutes=1)
MIN_INTERVAL = timedelta(seconds=30)

# Files with this suffix carry no payload and are skipped.
NO_CONTENT_SUFFIX = "_no_content_found.zip"
