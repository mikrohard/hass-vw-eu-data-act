"""Coordinator: dynamic-interval refresh of the latest dataset."""

from __future__ import annotations

import logging
import asyncio
from datetime import datetime, timezone

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import ApiError, AuthError, EudaApiClient
from .const import (
    CONF_IDENTIFIER,
    CONF_VIN,
    DATASET_INTERVAL,
    DOMAIN,
    MIN_INTERVAL,
    NO_CONTENT_SUFFIX,
    POST_DATASET_BUFFER,
    RETRY_INTERVAL,
)
from .data import Dataset, DataPoint

_LOGGER = logging.getLogger(__name__)


def _filename_timestamp(name: str) -> datetime | None:
    """Parse a YYYYMMDDhhmmss segment from a dataset filename.

    Handles both layouts seen in the wild ("TIMESTAMP_VIN.zip" and
    "VIN_TIMESTAMP.zip") by scanning the underscore-separated parts
    right-to-left for the first one that parses as a timestamp.
    """
    stem = name.rsplit(".", 1)[0]
    for part in reversed(stem.split("_")):
        try:
            return datetime.strptime(part, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _created_on(entry: dict) -> datetime | None:
    raw = entry.get("createdOn")
    if not raw:
        return _filename_timestamp(entry.get("name", ""))
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return _filename_timestamp(entry.get("name", ""))


class EudaCoordinator(DataUpdateCoordinator[dict[str, DataPoint]]):
    """Fetches the latest dataset and reschedules adaptively."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, client: EudaApiClient
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} {entry.data[CONF_VIN]}",
            update_interval=RETRY_INTERVAL,
        )
        self.entry = entry
        self.client = client
        self.vin: str = entry.data[CONF_VIN]
        self.identifier: str = entry.data[CONF_IDENTIFIER]
        self.latest_dataset: Dataset | None = None

    async def _async_update_data(self) -> dict[str, DataPoint]:
        listing = await self._async_list_with_refresh()

        # content datasets, oldest -> newest by createdOn
        content = sorted(
            (
                e
                for e in listing
                if e.get("name") and not e["name"].endswith(NO_CONTENT_SUFFIX)
            ),
            key=lambda e: _created_on(e) or datetime.min.replace(tzinfo=timezone.utc),
        )
        _LOGGER.debug("refresh: %d listed, %d with content", len(listing), len(content))

        if not content:
            self._reschedule(listing)
            if self.data:
                _LOGGER.debug("No new datasets available, keeping previous data")
                return self.data
            _LOGGER.warning(
                "No datasets available yet, will retry in %s", RETRY_INTERVAL
            )
            return {}

        # Try to load datasets, starting with newest and falling back to older ones
        last_error = None
        for dataset_entry in reversed(content):
            # Retry mechanism for transient server errors
            max_retries = 5
            retry_delay = 10  # seconds

            for attempt in range(max_retries):
                try:
                    payload = await self.client.async_download_dataset(
                        self.vin, self.identifier, dataset_entry["name"]
                    )
                    self.latest_dataset = Dataset.from_json(payload)
                    last_error = None
                    break  # Success!
                except ApiError as err:
                    last_error = err
                    is_server_error = any(
                        code in str(err)
                        for code in ["HTTP 500", "HTTP 502", "HTTP 503", "HTTP 504"]
                    )

                    if is_server_error and attempt < max_retries - 1:
                        _LOGGER.warning(
                            "Server error downloading %s (attempt %d/%d): %s, retrying in %ds",
                            dataset_entry["name"],
                            attempt + 1,
                            max_retries,
                            err,
                            retry_delay,
                        )
                        await asyncio.sleep(retry_delay)
                        continue
                    elif is_server_error:
                        _LOGGER.warning(
                            "Server error downloading %s after %d attempts: %s, trying previous dataset",
                            dataset_entry["name"],
                            max_retries,
                            err,
                        )
                        break
                    else:
                        _LOGGER.warning(
                            "Error downloading %s: %s", dataset_entry["name"], err
                        )
                        break

            if last_error is None:
                break

            if last_error and not any(
                code in str(last_error)
                for code in ["HTTP 500", "HTTP 502", "HTTP 503", "HTTP 504"]
            ):
                break

        # If all downloads failed
        if last_error:
            self.update_interval = RETRY_INTERVAL
            if self.data:
                _LOGGER.warning(
                    "Could not download any dataset (last error: %s), keeping previous data",
                    last_error,
                )
                self._reschedule(listing)
                return self.data
            _LOGGER.error(
                "Could not download any dataset on first load: %s. Integration will load "
                "but entities remain unavailable until data arrives. Retrying in %s",
                last_error,
                RETRY_INTERVAL,
            )
            return {}

        self._reschedule(listing)

        if self.data:
            merged = dict(self.data)
            merged.update(self.latest_dataset.points)
            return merged

        return self.latest_dataset.points

    async def _async_list_with_refresh(self) -> list[dict]:
        """List datasets, self-healing a stale identifier once if needed.

        If the user deletes and recreates the continuous data subscription on
        the portal, the backend assigns a new identifier and the stored one
        stops working (the list errors or returns no files). Re-fetch the
        identifier from the metadata endpoint and retry once before giving up —
        so it recovers on the next cycle without needing a manual reload.
        """
        for retried in (False, True):
            try:
                listing = await self.client.async_list_datasets(
                    self.vin, self.identifier
                )
            except AuthError as err:
                self.update_interval = RETRY_INTERVAL
                raise UpdateFailed(f"Authentication failed: {err}") from err
            except ApiError as err:
                if not retried and await self._refresh_identifier():
                    continue
                self.update_interval = RETRY_INTERVAL
                if "HTTP 400" in str(err):
                    # The data-delivery endpoint returns 400 until the portal
                    # finishes provisioning a newly enabled data request, which
                    # can take a few hours. HA keeps retrying until it's ready.
                    raise UpdateFailed(
                        "Data delivery not ready yet (HTTP 400). If you just enabled "
                        "the continuous data request on the portal, it can take a few "
                        "hours to start; will keep retrying."
                    ) from err
                raise UpdateFailed(str(err)) from err
            # An empty listing can also mean the subscription was recreated.
            if not listing and not retried and await self._refresh_identifier():
                continue
            return listing
        return listing

    async def _refresh_identifier(self) -> bool:
        """Re-fetch the data-request identifier; persist it if it changed.

        Returns True (and updates the config entry) when the portal has handed
        out a new identifier, e.g. after the subscription was recreated.
        """
        try:
            meta = await self.client.async_get_metadata(self.vin)
        except ApiError as err:
            _LOGGER.debug("Could not refresh data-request identifier: %s", err)
            return False
        new_id = meta.get("Identifier") or meta.get("identifier")
        if not new_id or new_id == self.identifier:
            return False
        _LOGGER.warning(
            "Data-request identifier changed (%s -> %s); the portal subscription "
            "was likely recreated. Updating the config entry.",
            self.identifier,
            new_id,
        )
        self.identifier = new_id
        self.hass.config_entries.async_update_entry(
            self.entry, data={**self.entry.data, CONF_IDENTIFIER: new_id}
        )
        return True

    def _reschedule(self, listing: list[dict]) -> None:
        """Schedule the next poll for ~15 min after the newest known dataset.

        If that time has already passed (a new dataset is due but not yet
        present), poll every minute until it appears.
        """
        timestamps = [ts for e in listing if (ts := _created_on(e))]
        newest = max(timestamps) if timestamps else None
        if newest:
            target = newest + DATASET_INTERVAL + POST_DATASET_BUFFER
            delta = target - dt_util.utcnow()
            if delta > MIN_INTERVAL:
                self.update_interval = delta
                _LOGGER.debug("Next refresh in %s (after newest %s)", delta, newest)
                return
        # newest dataset is overdue (or unknown) -> short retry for the next drop
        self.update_interval = RETRY_INTERVAL
        _LOGGER.debug("Next dataset overdue; retrying in %s", RETRY_INTERVAL)
