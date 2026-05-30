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

# Brand is part of the OIDC state; VW passenger cars by default.
BRAND = "VOLKSWAGEN_PASSENGER_CARS"
CALLBACK_LOGIN_PATH = "/services/callbacklogin"

# OIDC: we build the authorize URL directly instead of using the portal's
# /services/redirect/authentication servlet, which returns HTTP 500 for
# non-browser clients (it depends on AEM browser session state).
OIDC_AUTHORIZE_URL = IDENTITY_BASE + "/oidc/v1/authorize"
OIDC_CLIENT_ID = "9b58543e-1c15-4193-91d5-8a14145bebb0@apps_vw-dilab_com"
OIDC_SCOPE = "openid cars profile"
OIDC_REDIRECT_URI = BASE_URL + "/login"
# state encodes country__language__brand (echoed back to the portal callback).
DEFAULT_COUNTRY = "si"
DEFAULT_LANGUAGE = "sl"
OIDC_STATE = f"{DEFAULT_COUNTRY}__{DEFAULT_LANGUAGE}__{BRAND}"

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

# Persisted storage
STORAGE_VERSION = 1
STORAGE_KEY = DOMAIN + "_{entry_id}"
