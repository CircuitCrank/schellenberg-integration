import logging

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.restore_state import RestoreEntity

from .const import (
    DOMAIN,
    MANUFACTURER,
    CONF_NAME,
    CONF_REMOTE_ID,
    CONF_SIGNAL_UP,
    CONF_SIGNAL_DOWN,
    CONF_SIGNAL_STOP,
    CONF_SIGNAL_ALL_UP,
    CONF_SIGNAL_ALL_DOWN,
    CONF_SIGNAL_ALL_STOP,
    SUBENTRY_TYPE_REMOTE,
    SUBENTRY_TYPE_SHUTTER,
)
from .usb import SchellenbergUSB

_LOGGER = logging.getLogger(__name__)

_COMMAND_MAP = {
    "01": "up",
    "02": "down",
    "00": "stop",
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    usb: SchellenbergUSB = hass.data[DOMAIN][entry.entry_id]

    async_add_entities([SchellenbergRawSensor(entry, usb)])

    for subentry in entry.subentries.values():
        if subentry.data.get("subentry_type") == SUBENTRY_TYPE_REMOTE:
            async_add_entities(
                [SchellenbergRemoteSensor(entry, subentry, usb)],
                config_subentry_id=subentry.subentry_id,
            )


def _build_signal_lookup(entry: ConfigEntry) -> dict[str, str]:
    """Build a pattern → shutter_name lookup from all shutter subentries."""
    lookup: dict[str, str] = {}
    for subentry in entry.subentries.values():
        if subentry.data.get("subentry_type") != SUBENTRY_TYPE_SHUTTER:
            continue
        name = subentry.data.get(CONF_NAME, subentry.title)
        for key in (CONF_SIGNAL_UP, CONF_SIGNAL_DOWN, CONF_SIGNAL_STOP,
                    CONF_SIGNAL_ALL_UP, CONF_SIGNAL_ALL_DOWN, CONF_SIGNAL_ALL_STOP):
            pattern = subentry.data.get(key)
            if pattern:
                lookup[pattern] = name
    return lookup


class SchellenbergRawSensor(SensorEntity):
    _attr_has_entity_name = True
    _attr_name = "USB Raw"
    _attr_icon = "mdi:console"

    def __init__(self, entry: ConfigEntry, usb: SchellenbergUSB) -> None:
        self._entry = entry
        self._usb = usb
        self._attr_unique_id = f"{entry.entry_id}_raw"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
        )
        self._attr_native_value: str | None = None
        self._attr_available = True

    async def async_added_to_hass(self) -> None:
        self._usb.register_raw_callback(self._on_raw)
        self._usb.register_disconnect_callback(self._on_disconnect)
        self._usb.register_reconnect_callback(self._on_reconnect)

    async def async_will_remove_from_hass(self) -> None:
        self._usb.unregister_raw_callback(self._on_raw)
        self._usb.unregister_disconnect_callback(self._on_disconnect)
        self._usb.unregister_reconnect_callback(self._on_reconnect)

    def _on_disconnect(self) -> None:
        self._attr_available = False
        self.async_write_ha_state()

    def _on_reconnect(self) -> None:
        self._attr_available = True
        self.async_write_ha_state()

    def _on_raw(self, line: str) -> None:
        self._attr_native_value = line
        self.async_write_ha_state()


class SchellenbergRemoteSensor(SensorEntity, RestoreEntity):
    _attr_has_entity_name = True
    _attr_name = None

    def __init__(self, entry: ConfigEntry, subentry, usb: SchellenbergUSB) -> None:
        self._entry = entry
        self._subentry = subentry
        self._usb = usb

        self._attr_unique_id = subentry.subentry_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, subentry.subentry_id)},
            name=subentry.data.get(CONF_NAME, subentry.title),
            manufacturer=MANUFACTURER,
        )

        self._attr_native_value: str | None = None
        self._attr_available = True
        self._extra: dict = {}

    @property
    def extra_state_attributes(self) -> dict:
        return self._extra

    async def async_added_to_hass(self) -> None:
        last_state = await self.async_get_last_state()
        if last_state and last_state.state not in (None, "unavailable", "unknown"):
            self._attr_native_value = last_state.state
            self._extra = {
                k: v for k, v in last_state.attributes.items()
                if k in ("remote_id", "command", "channel", "shutter_name")
            }
        self._usb.register_signal_callback(self._on_signal)
        self._usb.register_disconnect_callback(self._on_disconnect)
        self._usb.register_reconnect_callback(self._on_reconnect)

    async def async_will_remove_from_hass(self) -> None:
        self._usb.unregister_signal_callback(self._on_signal)
        self._usb.unregister_disconnect_callback(self._on_disconnect)
        self._usb.unregister_reconnect_callback(self._on_reconnect)

    def _on_disconnect(self) -> None:
        self._attr_available = False
        self.async_write_ha_state()

    def _on_reconnect(self) -> None:
        self._attr_available = True
        self.async_write_ha_state()

    def _on_signal(self, raw: str) -> None:
        if len(raw) < 12:
            return
        if raw[10:12] not in ("01", "00", "02"):
            return
        remote_id = raw[4:10]
        if remote_id != self._subentry.data.get(CONF_REMOTE_ID):
            return

        pattern = raw[2:4] + raw[10:12]
        command = _COMMAND_MAP.get(raw[10:12])

        lookup = _build_signal_lookup(self._entry)
        shutter_name = lookup.get(pattern)

        self._attr_native_value = raw
        self._extra = {
            "remote_id": remote_id,
            "command": command,
            "channel": raw[2:4],
        }
        if shutter_name:
            self._extra["shutter_name"] = shutter_name

        self.async_write_ha_state()