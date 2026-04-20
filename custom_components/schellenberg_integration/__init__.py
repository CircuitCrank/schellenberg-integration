import logging
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.const import Platform

from .const import DOMAIN, CONF_SERIAL_PORT, DEFAULT_BAUD_RATE, MANUFACTURER
from .usb import SchellenbergUSB

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.COVER, Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    usb = SchellenbergUSB(
        port=entry.data[CONF_SERIAL_PORT],
        baud_rate=DEFAULT_BAUD_RATE,
    )

    try:
        connected = await usb.connect()
    except Exception as exc:
        raise ConfigEntryNotReady(
            f"Cannot connect to Schellenberg USB stick on {entry.data[CONF_SERIAL_PORT]}"
        ) from exc

    if not connected:
        raise ConfigEntryNotReady(
            f"Cannot connect to Schellenberg USB stick on {entry.data[CONF_SERIAL_PORT]}"
        )

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = usb

    device_registry = dr.async_get(hass)
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        name=entry.title,
        manufacturer=MANUFACTURER,
        model="Smart Home Funk-Stick",
    )

    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception:
        hass.data[DOMAIN].pop(entry.entry_id)
        await usb.disconnect()
        raise ConfigEntryNotReady("Failed to set up Schellenberg platforms")

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        usb: SchellenbergUSB = hass.data[DOMAIN].pop(entry.entry_id)
        await usb.disconnect()

    return unload_ok


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)