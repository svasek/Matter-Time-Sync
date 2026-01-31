"""Matter Time Sync button entities (with temporary grey-out on press)."""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


def _slug(text: str) -> str:
    """Make a stable slug for entity_id suggestions."""
    cleaned = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return cleaned[:50] if len(cleaned) > 50 else cleaned


def _matches_filters(name: str, filters: list[str]) -> bool:
    """Return True if filters are empty, or any filter is a substring of name."""
    if not filters:
        return True
    lowered = name.lower()
    return any(f in lowered for f in filters)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create one 'Sync Time' button per matching Matter node."""
    entry_data = hass.data[DOMAIN][config_entry.entry_id]
    coordinator = entry_data["coordinator"]
    device_filters = entry_data.get("device_filters", [])
    only_time_sync = entry_data.get("only_time_sync_devices", True)

    # Store callback so other parts of the integration can add entities later
    entry_data["async_add_entities"] = async_add_entities

    nodes = await coordinator.async_get_matter_nodes()

    _LOGGER.info(
        "Matter Time Sync: %d nodes found; filters=%s; only_time_sync=%s",
        len(nodes),
        device_filters if device_filters else "[none]",
        only_time_sync,
    )

    # Track known nodes for discovery
    known_node_ids: set[int] = set()
    new_entities: list[MatterTimeSyncButton] = []

    skipped_filter: list[str] = []
    skipped_no_cluster: list[str] = []

    for node in nodes:
        node_id = node.get("node_id")
        if node_id is None:
            continue

        name = node.get("name") or f"Matter Node {node_id}"
        has_time_sync = bool(node.get("has_time_sync", False))

        if only_time_sync and not has_time_sync:
            skipped_no_cluster.append(f"{name} (node {node_id})")
            continue

        if not _matches_filters(name, device_filters):
            skipped_filter.append(f"{name} (node {node_id})")
            continue

        known_node_ids.add(node_id)

        new_entities.append(
            MatterTimeSyncButton(
                coordinator=coordinator,
                node_id=node_id,
                node_name=name,
                device_info=node.get("device_info"),
            )
        )

    if new_entities:
        async_add_entities(new_entities)
        _LOGGER.info("Added %d buttons", len(new_entities))
    else:
        if not nodes:
            _LOGGER.warning("No Matter devices found (check Matter Server connection).")
        elif skipped_no_cluster and not any(bool(n.get("has_time_sync", False)) for n in nodes):
            _LOGGER.warning("No devices with Time Sync support were found.")
        else:
            _LOGGER.warning("No devices matched the current selection criteria.")

    if skipped_no_cluster:
        _LOGGER.info("Skipped (no Time Sync cluster): %s", skipped_no_cluster)
    if skipped_filter:
        _LOGGER.info("Skipped (filter mismatch): %s", skipped_filter)

    entry_data["known_node_ids"] = known_node_ids


async def async_check_new_devices(hass: HomeAssistant, entry_id: str) -> int:
    """Discover new nodes and add buttons for them; returns count added."""
    entry_data = hass.data[DOMAIN].get(entry_id)
    if not entry_data:
        return 0

    coordinator = entry_data["coordinator"]
    device_filters = entry_data.get("device_filters", [])
    only_time_sync = entry_data.get("only_time_sync_devices", True)
    known_node_ids: set[int] = entry_data.get("known_node_ids", set())
    async_add_entities = entry_data.get("async_add_entities")

    if not async_add_entities:
        return 0

    nodes = await coordinator.async_get_matter_nodes()
    to_add: list[MatterTimeSyncButton] = []

    for node in nodes:
        node_id = node.get("node_id")
        if node_id is None or node_id in known_node_ids:
            continue

        name = node.get("name") or f"Matter Node {node_id}"
        has_time_sync = bool(node.get("has_time_sync", False))

        if only_time_sync and not has_time_sync:
            continue
        if not _matches_filters(name, device_filters):
            continue

        known_node_ids.add(node_id)
        to_add.append(
            MatterTimeSyncButton(
                coordinator=coordinator,
                node_id=node_id,
                node_name=name,
                device_info=node.get("device_info"),
            )
        )

    if to_add:
        async_add_entities(to_add)
        _LOGGER.info("Added %d newly discovered button(s)", len(to_add))

    entry_data["known_node_ids"] = known_node_ids
    return len(to_add)


class MatterTimeSyncButton(ButtonEntity):
    """Button that triggers a time sync and greys out briefly after press."""

    _attr_icon = "mdi:clock-sync"
    _attr_has_entity_name = False

    def __init__(
        self,
        coordinator,
        node_id: int,
        node_name: str,
        device_info: dict | None = None,
    ) -> None:
        self._coordinator = coordinator
        self._node_id = node_id
        self._node_name = node_name

        self._attr_unique_id = f"matter_time_sync_{node_id}"
        self._attr_name = f"{node_name} Sync Time"
        self.entity_id = f"button.{_slug(node_name)}_sync_time"

        # Grey-out mechanism: flip availability to False for a short time
        self._attr_available = True
        self._press_lock = asyncio.Lock()

        if device_info:
            self._attr_device_info = DeviceInfo(
                identifiers={(DOMAIN, str(node_id))},
                name=node_name,
                manufacturer=device_info.get("vendor_name", "Unknown"),
                model=device_info.get("product_name", "Matter Device"),
            )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "node_id": self._node_id,
            "device_name": self._node_name,
            "integration": DOMAIN,
        }

    async def async_press(self) -> None:
        # Prevent concurrent presses from overlapping
        if self._press_lock.locked():
            return

        async with self._press_lock:
            # Immediately grey out
            self._attr_available = False
            self.async_write_ha_state()

            try:
                _LOGGER.info(
                    "Syncing time for Matter node %s (%s)", self._node_id, self._node_name
                )
                ok = await self._coordinator.async_sync_time(self._node_id, endpoint=0)
                if ok:
                    _LOGGER.info(
                        "Time sync successful for %s (node %s)",
                        self._node_name,
                        self._node_id,
                    )
                else:
                    _LOGGER.error(
                        "Time sync failed for %s (node %s)",
                        self._node_name,
                        self._node_id,
                    )
            finally:
                # Keep it greyed out a bit even if the sync was fast
                await asyncio.sleep(2)
                self._attr_available = True
                self.async_write_ha_state()
