"""Coordinator: dynamic-interval refresh, historical backfill, statistics."""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timezone

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
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
    STORAGE_KEY,
    STORAGE_VERSION,
)
from .data import CURATED_SENSORS, Dataset, DataPoint

_LOGGER = logging.getLogger(__name__)

# Curated fields we backfill into long-term statistics: numeric "measurement"
# sensors only. total_increasing sensors (e.g. mileage) are left to the
# recorder's own sum-based statistics to avoid conflicting metadata.
_NUMERIC_CURATED = {
    s.field_name: s
    for s in CURATED_SENSORS
    if s.unit and s.state_class == "measurement"
}

# Maps a curated unit to its HA statistics unit_class (conversion dimension).
# Units with no converter (e.g. percentage) use None.
_UNIT_CLASS: dict[str, str | None] = {
    "kW": "power",
    "°C": "temperature",
    "km": "distance",
    "s": "duration",
    "%": None,
}


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
    """Fetches datasets, backfills history, and reschedules adaptively."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: EudaApiClient) -> None:
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
        self._store: Store = Store(hass, STORAGE_VERSION, STORAGE_KEY.format(entry_id=entry.entry_id))
        self._ingested: set[str] = set()
        self.entities_ready = False
        # pending historical numeric points: field -> list[(timestamp, value)]
        self._pending_stats: dict[str, list[tuple[datetime, float]]] = defaultdict(list)
        self.latest_dataset: Dataset | None = None

    async def async_load_store(self) -> None:
        stored = await self._store.async_load() or {}
        self._ingested = set(stored.get("ingested", []))

    async def _save_store(self) -> None:
        await self._store.async_save({"ingested": sorted(self._ingested)[-100:]})

    async def _async_update_data(self) -> dict[str, DataPoint]:
        try:
            listing = await self.client.async_list_datasets(self.vin, self.identifier)
        except AuthError as err:
            raise UpdateFailed(f"Authentication failed: {err}") from err
        except ApiError as err:
            raise UpdateFailed(str(err)) from err

        # content datasets, oldest -> newest by createdOn
        content = sorted(
            (e for e in listing if e.get("name") and not e["name"].endswith(NO_CONTENT_SUFFIX)),
            key=lambda e: _created_on(e) or datetime.min.replace(tzinfo=timezone.utc),
        )
        _LOGGER.debug(
            "refresh: %d listed, %d with content, %d already ingested",
            len(listing), len(content), len(self._ingested),
        )

        if not content:
            self._reschedule(listing)
            if self.data:
                return self.data
            raise UpdateFailed("No datasets with content available yet")

        newest = content[-1]
        new_files = [e for e in content if e["name"] not in self._ingested]

        # 1. Live state: ALWAYS (re)load the newest dataset so entities reflect
        #    it, even when everything has already been ingested (e.g. after a
        #    restart with persisted state). This is independent of statistics.
        try:
            payload = await self.client.async_download_dataset(
                self.vin, self.identifier, newest["name"]
            )
            self.latest_dataset = Dataset.from_json(payload)
        except ApiError as err:
            self._reschedule(listing)
            if self.data:
                _LOGGER.warning("Could not download newest %s: %s", newest["name"], err)
                return self.data
            raise UpdateFailed(f"Could not download newest dataset: {err}") from err

        # 2. Backfill statistics for any not-yet-ingested datasets (oldest first).
        for e in new_files:
            if e["name"] == newest["name"]:
                ds = self.latest_dataset  # reuse the one just downloaded
            else:
                try:
                    payload = await self.client.async_download_dataset(
                        self.vin, self.identifier, e["name"]
                    )
                except ApiError as err:
                    _LOGGER.warning("Skipping %s: %s", e["name"], err)
                    continue
                ds = Dataset.from_json(payload)
            self._collect_stats(ds, fallback_ts=_created_on(e))
            self._ingested.add(e["name"])

        if new_files:
            await self._save_store()

        if self.entities_ready:
            self._write_statistics()

        self._reschedule(listing)
        return self.latest_dataset.points

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

    # -- statistics --------------------------------------------------------

    def _collect_stats(self, ds: Dataset, fallback_ts: datetime | None) -> None:
        ts = ds.captured_at or fallback_ts
        if ts is None:
            return
        for field_name, curated in _NUMERIC_CURATED.items():
            dp = ds.by_field(field_name)
            if dp is None:
                continue
            val = dp.value
            if curated.transform == "duration_s" and isinstance(val, str):
                continue
            if isinstance(val, bool) or not isinstance(val, (int, float)):
                continue
            self._pending_stats[field_name].append((ts, float(val)))

    def _write_statistics(self) -> None:
        """Flush pending numeric history into HA long-term statistics."""
        if not self._pending_stats:
            return
        try:
            from homeassistant.components.recorder.models import (
                StatisticData,
                StatisticMetaData,
            )
            from homeassistant.components.recorder.statistics import (
                async_import_statistics,
            )
            from homeassistant.helpers import entity_registry as er
        except ImportError:
            return

        # HA 2025.x replaced the boolean has_mean with mean_type; fall back to
        # has_mean on older versions that lack StatisticMeanType.
        try:
            from homeassistant.components.recorder.models import StatisticMeanType

            mean_meta = {"mean_type": StatisticMeanType.ARITHMETIC}
        except ImportError:
            mean_meta = {"has_mean": True}

        registry = er.async_get(self.hass)
        pending = self._pending_stats
        self._pending_stats = defaultdict(list)

        for field_name, points in pending.items():
            curated = _NUMERIC_CURATED[field_name]
            entity_id = registry.async_get_entity_id(
                "sensor", DOMAIN, f"{self.vin}_{field_name}"
            )
            if not entity_id:
                # entity not created yet; requeue for next flush
                self._pending_stats[field_name].extend(points)
                continue

            # bucket by hour (HA long-term statistics are hourly)
            buckets: dict[datetime, list[float]] = defaultdict(list)
            for ts, val in points:
                hour = ts.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
                buckets[hour].append(val)

            stats = [
                StatisticData(
                    start=hour,
                    mean=sum(vals) / len(vals),
                    min=min(vals),
                    max=max(vals),
                )
                for hour, vals in sorted(buckets.items())
            ]
            metadata = StatisticMetaData(
                has_sum=False,
                name=None,
                source="recorder",
                statistic_id=entity_id,
                unit_of_measurement=curated.unit,
                unit_class=_UNIT_CLASS.get(curated.unit),
                **mean_meta,
            )
            async_import_statistics(self.hass, metadata, stats)
            _LOGGER.debug("Imported %d hourly stats for %s", len(stats), entity_id)

    async def async_flush_statistics(self) -> None:
        """Called once after platforms are set up to backfill initial history."""
        self.entities_ready = True
        self._write_statistics()
