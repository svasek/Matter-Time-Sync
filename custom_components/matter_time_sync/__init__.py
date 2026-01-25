"""Matter Time Sync integration for Home Assistant."""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import async_track_time_interval

from .const import (
    DOMAIN,
    PLATFORMS,
    CONF_WS_URL,
    CONF_TIMEZONE,
    CONF_DEVICE_FILTER,
    CONF_AUTO_SYNC_ENABLED,
    CONF_AUTO_SYNC_INTERVAL,
    CONF_ONLY_TIME_SYNC_DEVICES,
    DEFAULT_AUTO_SYNC_ENABLED,
    DEFAULT_AUTO_SYNC_INTERVAL,
    DEFAULT_DEVICE_FILTER,
    DEFAULT_ONLY_TIME_SYNC_DEVICES,
)
from .coordinator import MatterTimeSyncCoordinator
from .button import async_check_new_devices

_LOGGER = logging.getLogger(__name__)

# Service schema
SERVICE_SYNC_TIME_SCHEMA = vol.Schema(
    {
        vol.Required("node_id"): cv.positive_int,
        vol.Optional("endpoint", default=0): cv.positive_int,
    }
)

SERVICE_SYNC_ALL_SCHEMA = vol.Schema({})


def parse_device_filter(filter_string: str) -> list[str]:
    """
    Parse the device filter string into a list of filter terms.
    
    Input: "alpstuga, sonoff_clock, IKEA"
    Output: ["alpstuga", "sonoff_clock", "ikea"]
    
    Empty string or None returns empty list (= no filter, all devices).
    """
    if not filter_string:
        return []
    
    # Split by comma, strip whitespace, convert to lowercase
    filters = [
        term.strip().lower() 
        for term in filter_string.split(",") 
        if term.strip()
    ]
    
    return filters


def device_matches_filter(device_name: str, filters: list[str]) -> bool:
    """
    Check if a device name matches any of the filter terms.
    
    Uses case-insensitive partial matching:
    - Filter "alpstuga" matches "IKEA ALPSTUGA Wohnzimmer"
    - Filter "ikea" matches "IKEA SYMFONISK"
    
    If filters is empty, all devices match.
    """
    if not filters:
        return True  # No filter = all devices
    
    device_name_lower = device_name.lower()
    
    for filter_term in filters:
        if filter_term in device_name_lower:
            return True
    
    return False


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Matter Time Sync from a config entry."""
    
    # Create coordinator
    coordinator = MatterTimeSyncCoordinator(hass, entry)
    
    # Try to connect
    if not await coordinator.async_connect():
        _LOGGER.warning(
            "Could not connect to Matter Server at startup, will retry later"
        )
    
    # Parse device filter
    filter_string = entry.data.get(CONF_DEVICE_FILTER, DEFAULT_DEVICE_FILTER)
    device_filters = parse_device_filter(filter_string)
    
    # Get only_time_sync_devices option
    only_time_sync = entry.data.get(CONF_ONLY_TIME_SYNC_DEVICES, DEFAULT_ONLY_TIME_SYNC_DEVICES)
    
    # Store coordinator and settings
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {
        "coordinator": coordinator,
        "device_filters": device_filters,
        "only_time_sync_devices": only_time_sync,
        "auto_sync_cancel": None,  # Will hold the cancel callback
        "known_node_ids": set(),  # Track known nodes for auto-discovery
        "async_add_entities": None,  # Will be set by button.py
    }
    
    # Set up platforms (button entities)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    
    # Register services
    await async_setup_services(hass)
    
    # Set up auto-sync if enabled
    await async_setup_auto_sync(hass, entry)
    
    # Register update listener for options
    entry.async_on_unload(entry.add_update_listener(async_update_options))
    
    _LOGGER.info(
        "Matter Time Sync loaded. Filter: %s, Auto-sync: %s",
        device_filters if device_filters else "all devices",
        entry.data.get(CONF_AUTO_SYNC_ENABLED, DEFAULT_AUTO_SYNC_ENABLED)
    )
    
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    # Cancel auto-sync timer
    entry_data = hass.data[DOMAIN].get(entry.entry_id, {})
    if entry_data.get("auto_sync_cancel"):
        entry_data["auto_sync_cancel"]()
    
    # Unload platforms
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    
    if unload_ok:
        # Disconnect coordinator
        data = hass.data[DOMAIN].pop(entry.entry_id)
        coordinator = data["coordinator"]
        await coordinator.async_disconnect()
    
    # Remove services if no more entries
    if not hass.data[DOMAIN]:
        hass.services.async_remove(DOMAIN, "sync_time")
        hass.services.async_remove(DOMAIN, "sync_all")
    
    return unload_ok


async def async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update."""
    entry_data = hass.data[DOMAIN][entry.entry_id]
    coordinator = entry_data["coordinator"]
    
    # Update coordinator config
    coordinator.update_config(
        ws_url=entry.data.get(CONF_WS_URL),
        timezone=entry.data.get(CONF_TIMEZONE),
    )
    
    # Update device filters
    filter_string = entry.data.get(CONF_DEVICE_FILTER, DEFAULT_DEVICE_FILTER)
    entry_data["device_filters"] = parse_device_filter(filter_string)
    
    # Update only_time_sync_devices option
    entry_data["only_time_sync_devices"] = entry.data.get(
        CONF_ONLY_TIME_SYNC_DEVICES, DEFAULT_ONLY_TIME_SYNC_DEVICES
    )
    
    # Cancel existing auto-sync
    if entry_data.get("auto_sync_cancel"):
        entry_data["auto_sync_cancel"]()
        entry_data["auto_sync_cancel"] = None
    
    # Set up new auto-sync if enabled
    await async_setup_auto_sync(hass, entry)
    
    # Reconnect with new settings
    await coordinator.async_disconnect()
    await coordinator.async_connect()
    
    # Reload the integration to apply filter changes to entities
    await hass.config_entries.async_reload(entry.entry_id)


async def async_setup_auto_sync(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Set up automatic time synchronization timer."""
    auto_sync_enabled = entry.data.get(CONF_AUTO_SYNC_ENABLED, DEFAULT_AUTO_SYNC_ENABLED)
    
    if not auto_sync_enabled:
        _LOGGER.debug("Auto-sync is disabled")
        return
    
    interval_minutes = entry.data.get(CONF_AUTO_SYNC_INTERVAL, DEFAULT_AUTO_SYNC_INTERVAL)
    interval = timedelta(minutes=interval_minutes)
    
    async def auto_sync_callback(now) -> None:
        """Callback for automatic time sync."""
        _LOGGER.debug("Auto-sync triggered (interval: %d minutes)", interval_minutes)
        
        entry_data = hass.data[DOMAIN].get(entry.entry_id)
        if not entry_data:
            return
        
        coordinator = entry_data["coordinator"]
        if not coordinator.is_connected:
            _LOGGER.warning("Auto-sync skipped: not connected to Matter Server")
            await coordinator.async_connect()
            return
        
        # First, check for new devices and add them
        new_devices = await async_check_new_devices(hass, entry.entry_id)
        if new_devices > 0:
            _LOGGER.info("Auto-discovery: Added %d new device(s)", new_devices)
        
        # Then sync all filtered nodes
        await sync_all_filtered_nodes(hass, entry.entry_id)
    
    # Start the timer
    cancel_callback = async_track_time_interval(
        hass,
        auto_sync_callback,
        interval,
    )
    
    # Store the cancel callback
    hass.data[DOMAIN][entry.entry_id]["auto_sync_cancel"] = cancel_callback
    
    _LOGGER.info(
        "Auto-sync enabled with %d minute interval",
        interval_minutes
    )
    
    # Also run once at startup (after a short delay)
    hass.async_create_task(
        async_initial_sync(hass, entry.entry_id)
    )


async def async_initial_sync(hass: HomeAssistant, entry_id: str) -> None:
    """Run initial sync after startup."""
    import asyncio
    
    # Wait a bit for everything to be ready
    await asyncio.sleep(30)
    
    entry_data = hass.data[DOMAIN].get(entry_id)
    if not entry_data:
        return
    
    coordinator = entry_data["coordinator"]
    if coordinator.is_connected:
        _LOGGER.info("Running initial auto-sync after startup")
        
        # Check for new devices (shouldn't find any at initial startup, but good for consistency)
        await async_check_new_devices(hass, entry_id)
        
        # Sync all nodes
        await sync_all_filtered_nodes(hass, entry_id)


async def sync_all_filtered_nodes(hass: HomeAssistant, entry_id: str) -> int:
    """Sync time on all filtered nodes. Returns number of synced nodes."""
    entry_data = hass.data[DOMAIN].get(entry_id)
    if not entry_data:
        return 0
    
    coordinator = entry_data["coordinator"]
    device_filters = entry_data.get("device_filters", [])
    only_time_sync = entry_data.get("only_time_sync_devices", True)
    
    # Get all nodes
    nodes = await coordinator.async_get_matter_nodes()
    
    synced_count = 0
    for node in nodes:
        node_name = node.get("name", f"Node {node['node_id']}")
        
        # Check if device has Time Sync support (if option is enabled)
        if only_time_sync and not node.get("has_time_sync", False):
            _LOGGER.debug("Skipping node %s (no Time Sync support)", node_name)
            continue
        
        # Check filter
        if not device_matches_filter(node_name, device_filters):
            _LOGGER.debug("Skipping node %s (does not match filter)", node_name)
            continue
        
        # Sync this node
        success = await coordinator.async_sync_time(node["node_id"], endpoint=0)
        if success:
            synced_count += 1
            _LOGGER.debug("Auto-synced time for %s", node_name)
        else:
            _LOGGER.warning("Failed to auto-sync time for %s", node_name)
    
    _LOGGER.info("Auto-sync complete: %d nodes synced", synced_count)
    return synced_count


async def async_setup_services(hass: HomeAssistant) -> None:
    """Set up services for Matter Time Sync."""
    
    async def handle_sync_time(call: ServiceCall) -> None:
        """Handle sync_time service call."""
        node_id = call.data["node_id"]
        endpoint = call.data.get("endpoint", 0)
        
        # Find a coordinator to use
        for entry_data in hass.data[DOMAIN].values():
            coordinator = entry_data["coordinator"]
            if coordinator.is_connected:
                success = await coordinator.async_sync_time(node_id, endpoint)
                if success:
                    _LOGGER.info("Time synced for node %s", node_id)
                else:
                    _LOGGER.error("Failed to sync time for node %s", node_id)
                return
        
        _LOGGER.error("No connected Matter Time Sync coordinator found")
    
    async def handle_sync_all(call: ServiceCall) -> None:
        """Handle sync_all service call - syncs all filtered devices."""
        total_synced = 0
        
        for entry_id in hass.data[DOMAIN]:
            synced = await sync_all_filtered_nodes(hass, entry_id)
            total_synced += synced
        
        _LOGGER.info("sync_all complete: %d nodes synced", total_synced)
    
    # Register services if not already registered
    if not hass.services.has_service(DOMAIN, "sync_time"):
        hass.services.async_register(
            DOMAIN,
            "sync_time",
            handle_sync_time,
            schema=SERVICE_SYNC_TIME_SCHEMA,
        )
    
    if not hass.services.has_service(DOMAIN, "sync_all"):
        hass.services.async_register(
            DOMAIN,
            "sync_all",
            handle_sync_all,
            schema=SERVICE_SYNC_ALL_SCHEMA,
        )
    
    async def handle_refresh_devices(call: ServiceCall) -> None:
        """Handle refresh_devices service call - checks for new devices."""
        total_new = 0
        
        for entry_id in hass.data[DOMAIN]:
            new_devices = await async_check_new_devices(hass, entry_id)
            total_new += new_devices
        
        if total_new > 0:
            _LOGGER.info("refresh_devices: Added %d new device(s)", total_new)
        else:
            _LOGGER.info("refresh_devices: No new devices found")
    
    if not hass.services.has_service(DOMAIN, "refresh_devices"):
        hass.services.async_register(
            DOMAIN,
            "refresh_devices",
            handle_refresh_devices,
            schema=SERVICE_SYNC_ALL_SCHEMA,  # No parameters needed
        )
