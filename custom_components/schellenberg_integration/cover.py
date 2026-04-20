import asyncio
import logging
import time

from homeassistant.components.cover import (
    CoverEntity,
    CoverEntityFeature,
    CoverDeviceClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.storage import Store

from .const import (
    DOMAIN,
    MANUFACTURER,
    CONF_CHANNEL,
    CONF_NAME,
    CONF_SEND_REPEAT,
    CONF_TRAVEL_TIME_UP,
    CONF_TRAVEL_TIME_DOWN,
    CONF_SIGNAL_UP,
    CONF_SIGNAL_DOWN,
    CONF_SIGNAL_STOP,
    CONF_SIGNAL_ALL_UP,
    CONF_SIGNAL_ALL_DOWN,
    CONF_SIGNAL_ALL_STOP,
    CONF_LAST_POSITION,
    DEFAULT_SEND_REPEAT,
    DEFAULT_TRAVEL_TIME,
    CMD_UP,
    CMD_DOWN,
    CMD_STOP,
    POSITION_OPEN,
    POSITION_CLOSED,
    SUBENTRY_TYPE_SHUTTER,
    SUBENTRY_TYPE_ALL,
)
from .usb import SchellenbergUSB

_LOGGER = logging.getLogger(__name__)

COVER_REGISTRY_KEY = "covers"
STORAGE_KEY_PREFIX = "schellenberg"
STORAGE_VERSION = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    usb: SchellenbergUSB = hass.data[DOMAIN][entry.entry_id]

    domain_data = hass.data[DOMAIN]
    if COVER_REGISTRY_KEY not in domain_data:
        domain_data[COVER_REGISTRY_KEY] = {}
    if entry.entry_id not in domain_data[COVER_REGISTRY_KEY]:
        domain_data[COVER_REGISTRY_KEY][entry.entry_id] = {}

    raw_store = Store(hass, STORAGE_VERSION, f"{STORAGE_KEY_PREFIX}_{entry.entry_id}")
    position_store = PositionStore(raw_store)
    stored_positions = await position_store.load_all()

    all_subentries = []

    for subentry in entry.subentries.values():
        subentry_type = subentry.data.get("subentry_type")

        if subentry_type == SUBENTRY_TYPE_SHUTTER:
            cover = SchellenbergCover(
                entry, subentry, usb, position_store,
                stored_positions.get(subentry.subentry_id),
            )
            domain_data[COVER_REGISTRY_KEY][entry.entry_id][subentry.subentry_id] = cover
            async_add_entities([cover], config_subentry_id=subentry.subentry_id)
        elif subentry_type == SUBENTRY_TYPE_ALL:
            all_subentries.append(subentry)

    for subentry in all_subentries:
        cover = SchellenbergAllCover(entry, subentry, usb)
        async_add_entities([cover], config_subentry_id=subentry.subentry_id)


def _extract_pattern(raw: str) -> str:
    return raw[2:4] + raw[10:12]


class SchellenbergCover(CoverEntity, RestoreEntity):
    _attr_device_class = CoverDeviceClass.SHUTTER
    _attr_supported_features = (
        CoverEntityFeature.OPEN
        | CoverEntityFeature.CLOSE
        | CoverEntityFeature.STOP
        | CoverEntityFeature.SET_POSITION
    )
    _attr_has_entity_name = True
    _attr_name = None
    _attr_available = True

    def __init__(
        self,
        entry: ConfigEntry,
        subentry,
        usb: SchellenbergUSB,
        store: Store,
        stored_position: int | None,
    ) -> None:
        self._entry = entry
        self._subentry = subentry
        self._usb = usb
        self._position_store = store
        self._stored_position = stored_position

        self._attr_unique_id = subentry.subentry_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, subentry.subentry_id)},
            name=subentry.data.get(CONF_NAME, subentry.title),
            manufacturer=MANUFACTURER,
        )

        self._position: int | None = None
        self._movement_task: asyncio.Task | None = None
        self._move_start_time: float | None = None
        self._move_start_position: int | None = None
        self._moving_direction: int | None = None

        # Send-worker state: decouples UI feedback from USB ACK-wait.
        # _desired_command holds the latest user intent; worker coalesces
        # rapid successive clicks so only the last command is actually sent.
        self._desired_command: int | None = None
        self._last_sent_command: int | None = None
        self._worker_event = asyncio.Event()
        self._worker_task: asyncio.Task | None = None
        self._worker_shutdown = False

    @property
    def _channel(self) -> int:
        return self._subentry.data[CONF_CHANNEL]

    @property
    def _send_repeat(self) -> int:
        return self._entry.options.get(
            CONF_SEND_REPEAT,
            self._entry.data.get(CONF_SEND_REPEAT, DEFAULT_SEND_REPEAT),
        )

    @property
    def _travel_time_up(self) -> float:
        return float(self._subentry.data.get(CONF_TRAVEL_TIME_UP, DEFAULT_TRAVEL_TIME))

    @property
    def _travel_time_down(self) -> float:
        return float(self._subentry.data.get(CONF_TRAVEL_TIME_DOWN, DEFAULT_TRAVEL_TIME))

    @property
    def current_cover_position(self) -> int | None:
        return self._position

    @property
    def is_closed(self) -> bool | None:
        if self._position is None:
            return None
        return self._position == POSITION_CLOSED

    @property
    def is_opening(self) -> bool:
        return self._moving_direction == CMD_UP

    @property
    def is_closing(self) -> bool:
        return self._moving_direction == CMD_DOWN

    async def async_added_to_hass(self) -> None:
        # Priority: RestoreState > Store > subentry.data fallback
        last_state = await self.async_get_last_state()
        if last_state and last_state.attributes.get("current_position") is not None:
            self._position = max(POSITION_CLOSED, min(POSITION_OPEN, int(last_state.attributes["current_position"])))
        elif self._stored_position is not None:
            self._position = self._stored_position
        elif (last := self._subentry.data.get(CONF_LAST_POSITION)) is not None:
            self._position = last

        self._usb.register_signal_callback(self._on_signal)
        self._usb.register_disconnect_callback(self._on_disconnect)
        self._usb.register_reconnect_callback(self._on_reconnect)

        self._worker_task = self._entry.async_create_background_task(
            self.hass, self._send_worker(), f"schellenberg_worker_{self._subentry.subentry_id}"
        )

    async def async_will_remove_from_hass(self) -> None:
        self._usb.unregister_signal_callback(self._on_signal)
        self._usb.unregister_disconnect_callback(self._on_disconnect)
        self._usb.unregister_reconnect_callback(self._on_reconnect)
        registry = self.hass.data.get(DOMAIN, {}).get(COVER_REGISTRY_KEY, {})
        registry.get(self._entry.entry_id, {}).pop(self._subentry.subentry_id, None)

        self._worker_shutdown = True
        self._worker_event.set()
        if self._worker_task and not self._worker_task.done():
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass

        await self._cancel_movement(send_stop=False)

    async def _save_position(self) -> None:
        if self._position is None:
            return
        await self._position_store.save(self._subentry.subentry_id, self._position)

    def _on_disconnect(self) -> None:
        self._attr_available = False
        self.hass.async_create_task(self._handle_disconnect())

    async def _handle_disconnect(self) -> None:
        if self._movement_task and not self._movement_task.done():
            self._movement_task.cancel()
            try:
                await self._movement_task
            except asyncio.CancelledError:
                pass
            if self._move_start_time is not None:
                elapsed = time.monotonic() - self._move_start_time
                travel_time = (
                    self._travel_time_up
                    if self._moving_direction == CMD_UP
                    else self._travel_time_down
                )
                self._position = self._interpolate(
                    self._move_start_position, self._moving_direction, elapsed, travel_time
                )
        self._moving_direction = None
        self._move_start_time = None
        self.async_write_ha_state()

    def _on_reconnect(self) -> None:
        self._attr_available = True
        self.hass.async_create_task(self._write_state_safe())

    async def _write_state_safe(self) -> None:
        self.async_write_ha_state()

    def _on_signal(self, raw: str) -> None:
        if len(raw) < 12:
            return
        data = self._subentry.data
        pattern = _extract_pattern(raw)

        if pattern in (data.get(CONF_SIGNAL_UP), data.get(CONF_SIGNAL_ALL_UP)):
            if self._moving_direction == CMD_UP:
                return
            self.hass.async_create_task(self.issue_command(POSITION_OPEN, send=False))
        elif pattern in (data.get(CONF_SIGNAL_DOWN), data.get(CONF_SIGNAL_ALL_DOWN)):
            if self._moving_direction == CMD_DOWN:
                return
            self.hass.async_create_task(self.issue_command(POSITION_CLOSED, send=False))
        elif pattern in (data.get(CONF_SIGNAL_STOP), data.get(CONF_SIGNAL_ALL_STOP)):
            if self._moving_direction is None:
                return
            self.hass.async_create_task(self.issue_stop(send=False))

    async def async_open_cover(self, **kwargs) -> None:
        await self.issue_command(POSITION_OPEN)

    async def async_close_cover(self, **kwargs) -> None:
        await self.issue_command(POSITION_CLOSED)

    async def async_stop_cover(self, **kwargs) -> None:
        await self.issue_stop()

    async def async_set_cover_position(self, position: int, **kwargs) -> None:
        await self.issue_command(position)

    async def issue_command(self, target: int, send: bool = True) -> None:
        if send and not self._usb.connected:
            _LOGGER.warning(
                "Ignoring command for %s: USB stick not connected",
                self._subentry.subentry_id,
            )
            return

        if self._position is None:
            self._position = POSITION_OPEN if target < 50 else POSITION_CLOSED

        if self._position == target:
            return

        # Cancel any running movement; interpolates _position to current value.
        await self._cancel_movement_no_lock(send_stop=False)

        current = self._position
        if current == target:
            return

        direction = CMD_UP if target > current else CMD_DOWN
        travel_time = self._travel_time_up if direction == CMD_UP else self._travel_time_down
        duration = (abs(target - current) / 100.0) * travel_time

        # Start movement simulation immediately so UI reflects the user's
        # intent without waiting for the USB ACK (~0.5-3s).
        self._moving_direction = direction
        self._move_start_time = time.monotonic()
        self._move_start_position = current
        self.async_write_ha_state()

        self._movement_task = self.hass.async_create_task(
            self._movement_loop(target, direction, duration, travel_time)
        )

        # Dispatch send via worker (coalesces rapid clicks).
        if send:
            self._desired_command = direction
            self._worker_event.set()

    async def issue_stop(self, send: bool = True) -> None:
        if send and not self._usb.connected:
            _LOGGER.warning(
                "Ignoring stop for %s: USB stick not connected",
                self._subentry.subentry_id,
            )
            return
        await self._cancel_movement_no_lock(send_stop=send)

    async def _movement_loop(
        self,
        target: int,
        direction: int,
        duration: float,
        travel_time: float,
    ) -> None:
        try:
            while True:
                await asyncio.sleep(0.5)
                start_time = self._move_start_time
                start_position = self._move_start_position
                if start_time is None or start_position is None:
                    raise asyncio.CancelledError
                elapsed = time.monotonic() - start_time
                self._position = self._interpolate(
                    start_position, direction, elapsed, travel_time
                )
                self.async_write_ha_state()
                if elapsed >= duration:
                    break
            self._position = target
        except asyncio.CancelledError:
            # Position is calculated by whoever cancelled
            raise

        self._moving_direction = None
        self._move_start_time = None
        self.async_write_ha_state()

        if target not in (POSITION_OPEN, POSITION_CLOSED):
            self._desired_command = CMD_STOP
            self._worker_event.set()
        await self._save_position()

    def _interpolate(self, start: int, direction: int, elapsed: float, travel_time: float) -> int:
        fraction = min(elapsed / travel_time, 1.0)
        delta = int(fraction * 100)
        if direction == CMD_UP:
            return min(start + delta, POSITION_OPEN)
        return max(start - delta, POSITION_CLOSED)

    async def _cancel_movement_no_lock(self, send_stop: bool = True) -> None:
        """Cancel running movement and optionally dispatch stop via worker."""
        task = self._movement_task
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            if self._move_start_time is not None:
                elapsed = time.monotonic() - self._move_start_time
                travel_time = (
                    self._travel_time_up
                    if self._moving_direction == CMD_UP
                    else self._travel_time_down
                )
                self._position = self._interpolate(
                    self._move_start_position, self._moving_direction, elapsed, travel_time
                )

        self._moving_direction = None
        self._move_start_time = None
        self.async_write_ha_state()
        await self._save_position()

        if send_stop:
            self._desired_command = CMD_STOP
            self._worker_event.set()

    async def _cancel_movement(self, send_stop: bool = True) -> None:
        """Public cancel entry point used by AllCover and lifecycle hooks."""
        await self._cancel_movement_no_lock(send_stop=send_stop)

    async def _send(self, command: int) -> bool:
        return await self._usb.send_command(
            enumerator=self._channel,
            command=command,
            repeat=self._send_repeat,
        )

    async def _send_worker(self) -> None:
        """Permanent task that serializes USB sends with coalescing.

        Coalescing: if _desired_command changes during an ACK-wait, only the
        final value is sent after the current send completes. Older targets
        are discarded — user intent is "the last click wins".
        """
        while not self._worker_shutdown:
            try:
                await self._worker_event.wait()
                self._worker_event.clear()

                while not self._worker_shutdown:
                    cmd = self._desired_command
                    if cmd is None or cmd == self._last_sent_command:
                        break

                    if not self._usb.connected:
                        _LOGGER.warning(
                            "Dropping send for %s: USB stick not connected",
                            self._subentry.subentry_id,
                        )
                        self._desired_command = None
                        break

                    result = await self._send(cmd)
                    if result:
                        self._last_sent_command = cmd
                    else:
                        _LOGGER.warning(
                            "Send failed for %s (command 0x%02X), waiting for next click",
                            self._subentry.subentry_id,
                            cmd,
                        )
                        self._desired_command = None
                        break
            except asyncio.CancelledError:
                break
            except Exception as exc:
                _LOGGER.exception("Send worker error for %s: %s", self._subentry.subentry_id, exc)


class SchellenbergAllCover(CoverEntity):
    _attr_device_class = CoverDeviceClass.SHUTTER
    _attr_supported_features = (
        CoverEntityFeature.OPEN
        | CoverEntityFeature.CLOSE
        | CoverEntityFeature.STOP
    )
    _attr_has_entity_name = True
    _attr_name = None
    _attr_available = True
    _attr_assumed_state = True

    def __init__(
        self,
        entry: ConfigEntry,
        subentry,
        usb: SchellenbergUSB,
    ) -> None:
        self._entry = entry
        self._subentry = subentry
        self._usb = usb

        self._attr_unique_id = subentry.subentry_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, subentry.subentry_id)},
            name=subentry.data.get(CONF_NAME, subentry.title),
            manufacturer=MANUFACTURER,
        )
        self._track_task: asyncio.Task | None = None

    @property
    def _channel(self) -> int:
        return self._subentry.data[CONF_CHANNEL]

    @property
    def _send_repeat(self) -> int:
        return self._entry.options.get(
            CONF_SEND_REPEAT,
            self._entry.data.get(CONF_SEND_REPEAT, DEFAULT_SEND_REPEAT),
        )

    @property
    def _covers(self) -> list:
        return list(
            self.hass.data.get(DOMAIN, {})
            .get(COVER_REGISTRY_KEY, {})
            .get(self._entry.entry_id, {})
            .values()
        )

    @property
    def is_closed(self) -> bool | None:
        positions = [
            c.current_cover_position
            for c in self._covers
            if c.current_cover_position is not None
        ]
        if not positions:
            return None
        return all(p == POSITION_CLOSED for p in positions)

    @property
    def is_opening(self) -> bool:
        return False

    @property
    def is_closing(self) -> bool:
        return False

    async def async_added_to_hass(self) -> None:
        self._usb.register_disconnect_callback(self._on_disconnect)
        self._usb.register_reconnect_callback(self._on_reconnect)

    async def async_will_remove_from_hass(self) -> None:
        self._usb.unregister_disconnect_callback(self._on_disconnect)
        self._usb.unregister_reconnect_callback(self._on_reconnect)

    def _on_disconnect(self) -> None:
        self._attr_available = False
        self.hass.async_create_task(self._write_state_safe())

    def _on_reconnect(self) -> None:
        self._attr_available = True
        self.hass.async_create_task(self._write_state_safe())

    async def _write_state_safe(self) -> None:
        self.async_write_ha_state()

    def _start_track(self, coro) -> None:
        if self._track_task and not self._track_task.done():
            self._track_task.cancel()
        self._track_task = self.hass.async_create_task(coro)

    async def async_open_cover(self, **kwargs) -> None:
        covers = list(self._covers)
        async def _track() -> None:
            await asyncio.gather(*[c.issue_command(POSITION_OPEN, send=False) for c in covers], return_exceptions=True)
        self._start_track(_track())
        await self._send(CMD_UP)

    async def async_close_cover(self, **kwargs) -> None:
        covers = list(self._covers)
        async def _track() -> None:
            await asyncio.gather(*[c.issue_command(POSITION_CLOSED, send=False) for c in covers], return_exceptions=True)
        self._start_track(_track())
        await self._send(CMD_DOWN)

    async def async_stop_cover(self, **kwargs) -> None:
        covers = list(self._covers)
        async def _track() -> None:
            await asyncio.gather(*[c.issue_stop() for c in covers], return_exceptions=True)
        self._start_track(_track())
        await self._send(CMD_STOP)

    async def _send(self, command: int) -> bool:
        return await self._usb.send_command(
            enumerator=self._channel,
            command=command,
            repeat=self._send_repeat,
        )
    
class PositionStore:
    def __init__(self, store: Store) -> None:
        self._store = store
        self._lock = asyncio.Lock()
        self._cache: dict[str, int] = {}

    async def load_all(self) -> dict[str, int]:
        async with self._lock:
            data = await self._store.async_load() or {}
            self._cache = data.get("positions", {})
            return dict(self._cache)

    async def save(self, subentry_id: str, position: int) -> None:
        async with self._lock:
            self._cache[subentry_id] = position
            try:
                await self._store.async_save({"positions": self._cache})
            except Exception:
                _LOGGER.warning("Failed to persist position for %s", subentry_id)