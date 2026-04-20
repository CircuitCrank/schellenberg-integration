import logging
import time
import asyncio

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
)

from .const import (
    DOMAIN,
    CONF_SERIAL_PORT,
    CONF_SEND_REPEAT,
    CONF_CHANNEL,
    CONF_NAME,
    CONF_TRAVEL_TIME_UP,
    CONF_TRAVEL_TIME_DOWN,
    CONF_SIGNAL_UP,
    CONF_SIGNAL_DOWN,
    CONF_SIGNAL_STOP,
    CONF_SIGNAL_ALL_UP,
    CONF_SIGNAL_ALL_DOWN,
    CONF_SIGNAL_ALL_STOP,
    CONF_LAST_POSITION,
    CONF_REMOTE_ID,
    DEFAULT_SEND_REPEAT,
    DEFAULT_TRAVEL_TIME,
    MIN_SEND_REPEAT,
    MAX_SEND_REPEAT,
    CMD_UP,
    CMD_DOWN,
    CMD_STOP,
    CMD_PAIR,
    CMD_PAIR_ALLOW,
    SUBENTRY_TYPE_SHUTTER,
    SUBENTRY_TYPE_ALL,
    SUBENTRY_TYPE_REMOTE,
    POSITION_OPEN,
    POSITION_CLOSED,
)
from .usb import SchellenbergUSB, get_available_serial_ports

_LOGGER = logging.getLogger(__name__)


def _get_usb(hass, entry_id) -> SchellenbergUSB | None:
    return hass.data.get(DOMAIN, {}).get(entry_id)

def _get_used_channels(entry) -> set[int]:
    return {
        se.data[CONF_CHANNEL]
        for se in entry.subentries.values()
        if CONF_CHANNEL in se.data
    }

def _get_shutter_subentries(entry) -> list:
    return [
        se for se in entry.subentries.values()
        if se.data.get("subentry_type") == SUBENTRY_TYPE_SHUTTER
    ]

def _get_all_subentry(entry) -> object | None:
    for se in entry.subentries.values():
        if se.data.get("subentry_type") == SUBENTRY_TYPE_ALL:
            return se
    return None

def _get_remote_subentries(entry) -> list:
    return [
        se for se in entry.subentries.values()
        if se.data.get("subentry_type") == SUBENTRY_TYPE_REMOTE
    ]

# ----------------------------------------------------------------------
# Options Flow
# ----------------------------------------------------------------------

class SchellenbergOptionsFlow(config_entries.OptionsFlow):

    async def async_step_init(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(data={
                CONF_SEND_REPEAT: int(user_input[CONF_SEND_REPEAT]),
             })

        schema = vol.Schema({
            vol.Required(
                CONF_SEND_REPEAT,
                default=self.config_entry.options.get(
                    CONF_SEND_REPEAT,
                    self.config_entry.data.get(CONF_SEND_REPEAT, DEFAULT_SEND_REPEAT),
                ),
            ): NumberSelector(NumberSelectorConfig(
                    min=MIN_SEND_REPEAT,
                    max=MAX_SEND_REPEAT,
                    mode=NumberSelectorMode.BOX,
                )),
        })

        return self.async_show_form(step_id="init", data_schema=schema)


# ----------------------------------------------------------------------
# Shutter Subentry Flow
# ----------------------------------------------------------------------

class SchellenbergShutterSubEntryFlow(config_entries.ConfigSubentryFlow):

    def __init__(self) -> None:
        self._data: dict = {}
        self._cal_start: float | None = None
        self._shutter_list: list = []
        self._current_index: int = 0
        self._last_signal: str | None = None
        self._signal_cb_registered: bool = False
        self._signal_queue: list[dict] = []
        self._current_queue_index: int = 0
        self._learned: dict = {}

    @property
    def config_entry(self):
        entry_id = self.handler[0] if isinstance(self.handler, tuple) else self.handler
        entry = self.hass.config_entries.async_get_entry(entry_id)
        if entry is None:
            raise ValueError(f"Config entry {entry_id} not found")
        return entry

    def _is_reconfigure(self) -> bool:
        try:
            self._get_reconfigure_subentry()
            return True
        except (AttributeError, KeyError, ValueError):
            return False

    def _existing(self, key, default=None):
        try:
            subentry = self._get_reconfigure_subentry()
            return subentry.data.get(key, default)
        except (AttributeError, KeyError, ValueError):
            return default

    def _send_repeat(self) -> int:
        entry = self.config_entry
        return entry.options.get(CONF_SEND_REPEAT, entry.data.get(CONF_SEND_REPEAT, DEFAULT_SEND_REPEAT))

    async def _send(self, channel: int, command: int) -> None:
        usb = _get_usb(self.hass, self.config_entry.entry_id)
        if usb:
            await usb.send_command(
                enumerator=channel,
                command=command,
                repeat=self._send_repeat(),
            )

    # --- Entry Points ---

    async def async_step_user(self, user_input=None):
        if self._is_reconfigure():
            return await self.async_step_channel(user_input)

        if user_input is not None:
            entry_type = user_input.get("entry_type")
            if entry_type == SUBENTRY_TYPE_ALL:
                shutters = _get_shutter_subentries(self.config_entry)
                if len(shutters) < 2:
                    return self.async_abort(reason="not_enough_shutters")
                if _get_all_subentry(self.config_entry) is not None:
                    return self.async_abort(reason="all_already_exists")
                return await self.async_step_all_channel()
            if entry_type == SUBENTRY_TYPE_REMOTE:
                return await self.async_step_remote_setup()
            return await self.async_step_setup_mode()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required("entry_type"): SelectSelector(SelectSelectorConfig(
                    options=[SUBENTRY_TYPE_SHUTTER, SUBENTRY_TYPE_ALL, SUBENTRY_TYPE_REMOTE],
                    mode=SelectSelectorMode.LIST,
                    translation_key="entry_type",
                )),
            }),
        )

    async def async_step_reconfigure(self, user_input=None):
        try:
            subentry = self._get_reconfigure_subentry()
        except (AttributeError, KeyError, ValueError):
            return self.async_abort(reason="unknown")

        for key in (CONF_NAME, CONF_CHANNEL, CONF_SIGNAL_UP, CONF_SIGNAL_DOWN, CONF_SIGNAL_STOP,
                    CONF_SIGNAL_ALL_UP, CONF_SIGNAL_ALL_DOWN, CONF_SIGNAL_ALL_STOP, CONF_REMOTE_ID):
            val = subentry.data.get(key)
            if val is not None:
                self._data[key] = val

        subentry_type = subentry.data.get("subentry_type")
        if subentry_type == SUBENTRY_TYPE_ALL:
            return await self.async_step_all_channel()
        if subentry_type == SUBENTRY_TYPE_REMOTE:
            return await self.async_step_remote_reconfigure_menu()

        return await self.async_step_reconfigure_menu()

    async def async_step_reconfigure_menu(self, user_input=None):
        if user_input is not None:
            action = user_input.get("reconfigure_action")
            if action == "calibration":
                self._data[CONF_CHANNEL] = self._existing(CONF_CHANNEL)
                self._data["subentry_type"] = SUBENTRY_TYPE_SHUTTER
                return await self.async_step_calibration_intro()
            if action == "pair_all":
                return await self.async_step_pair_all()
            return await self.async_step_channel()

        subentry_type = self._existing("subentry_type")
        options = ["channel", "calibration"]
        if subentry_type == SUBENTRY_TYPE_SHUTTER and _get_all_subentry(self.config_entry) is not None:
            options.append("pair_all")

        return self.async_show_form(
            step_id="reconfigure_menu",
            data_schema=vol.Schema({
                vol.Required("reconfigure_action"): SelectSelector(SelectSelectorConfig(
                    options=options,
                    mode=SelectSelectorMode.LIST,
                    translation_key="reconfigure_action",
                )),
            }),
        )

    # --- Shutter Flow ---

    async def async_step_setup_mode(self, user_input=None):
        if user_input is not None:
            if user_input.get("already_setup") != "yes":
                return self.async_abort(reason="motor_not_configured")
            self._data[CONF_NAME] = user_input[CONF_NAME]
            self._data["subentry_type"] = SUBENTRY_TYPE_SHUTTER
            return await self.async_step_pairing_status()

        return self.async_show_form(
            step_id="setup_mode",
            data_schema=vol.Schema({
                vol.Required(CONF_NAME): TextSelector(TextSelectorConfig()),
                vol.Required("already_setup"): SelectSelector(SelectSelectorConfig(
                    options=["yes", "no"],
                    mode=SelectSelectorMode.LIST,
                    translation_key="already_setup",
                )),
            }),
        )

    async def async_step_pairing_status(self, user_input=None):
        errors = {}

        if user_input is not None:
            channel_raw = user_input.get(CONF_CHANNEL, "").strip().upper()
            if not channel_raw or len(channel_raw) != 2:
                errors["base"] = "invalid_channel"
            else:
                try:
                    channel = int(channel_raw, 16)
                except ValueError:
                    errors["base"] = "invalid_channel"
                else:
                    used = _get_used_channels(self.config_entry)
                    if channel in used:
                        errors["base"] = "channel_in_use"
                    else:
                        self._data[CONF_CHANNEL] = channel
                        if user_input.get("already_paired") != "yes":
                            return await self.async_step_pair_quick()
                        return await self.async_step_calibration_intro()

        return self.async_show_form(
            step_id="pairing_status",
            data_schema=vol.Schema({
                vol.Required(CONF_CHANNEL): str,
                vol.Required("already_paired"): SelectSelector(SelectSelectorConfig(
                    options=["yes", "no"],
                    mode=SelectSelectorMode.LIST,
                    translation_key="already_paired",
                )),
            }),
            errors=errors,
        )

    async def async_step_channel(self, user_input=None):
        errors = {}
        is_reconf = self._is_reconfigure()

        if user_input is not None:
            if is_reconf:
                self._data[CONF_NAME] = user_input[CONF_NAME]
            channel_raw = user_input.get(CONF_CHANNEL, "").strip().upper()
            if not channel_raw or len(channel_raw) != 2:
                errors["base"] = "invalid_channel"
            else:
                try:
                    channel = int(channel_raw, 16)
                except ValueError:
                    errors["base"] = "invalid_channel"
                else:
                    used = _get_used_channels(self.config_entry)
                    existing_channel = self._existing(CONF_CHANNEL)
                    if channel in used and channel != existing_channel:
                        errors["base"] = "channel_in_use"
                    else:
                        self._data[CONF_CHANNEL] = channel
                        self._data["subentry_type"] = SUBENTRY_TYPE_SHUTTER
                        if self._is_reconfigure():
                            return await self.async_step_pairing_check()
                        return await self.async_step_calibration_intro()

        fields = {}
        if is_reconf:
            fields[vol.Required(CONF_NAME, default=self._existing(CONF_NAME, ""))] = str
        existing_ch = self._existing(CONF_CHANNEL)
        ch_default = format(existing_ch, "02X") if existing_ch is not None else ""
        fields[vol.Required(CONF_CHANNEL, default=ch_default)] = str

        return self.async_show_form(
            step_id="channel",
            data_schema=vol.Schema(fields),
            errors=errors,
        )

    async def async_step_pairing_check(self, user_input=None):
        if user_input is not None:
            if user_input.get("needs_pairing") == "yes":
                return await self.async_step_pair_quick_reconf()
            return self._finish_shutter()

        return self.async_show_form(
            step_id="pairing_check",
            data_schema=vol.Schema({
                vol.Required("needs_pairing"): SelectSelector(SelectSelectorConfig(
                    options=["yes", "no"],
                    mode=SelectSelectorMode.LIST,
                    translation_key="needs_pairing",
                )),
            }),
        )

    async def async_step_pair_quick_reconf(self, user_input=None):
        if user_input is not None:
            await self._send(self._data[CONF_CHANNEL], CMD_PAIR)
            return await self.async_step_pair_confirm_reconf()

        return self.async_show_form(
            step_id="pair_quick_reconf",
            data_schema=vol.Schema({}),
        )

    async def async_step_pair_confirm_reconf(self, user_input=None):
        if user_input is not None:
            if user_input.get("paired") != "yes":
                return self.async_abort(reason="pairing_failed")
            return self._finish_shutter()

        return self.async_show_form(
            step_id="pair_confirm_reconf",
            data_schema=vol.Schema({
                vol.Required("paired"): SelectSelector(SelectSelectorConfig(
                    options=["yes", "no"],
                    mode=SelectSelectorMode.LIST,
                    translation_key="paired",
                )),
            }),
        )

    async def async_step_pair_quick(self, user_input=None):
        if user_input is not None:
            await self._send(self._data[CONF_CHANNEL], CMD_PAIR)
            return await self.async_step_pair_confirm()

        return self.async_show_form(
            step_id="pair_quick",
            data_schema=vol.Schema({}),
        )

    async def async_step_pair_confirm(self, user_input=None):
        if user_input is not None:
            if user_input.get("paired") != "yes":
                return self.async_abort(reason="pairing_failed")
            return await self.async_step_calibration_intro()

        return self.async_show_form(
            step_id="pair_confirm",
            data_schema=vol.Schema({
                vol.Required("paired"): SelectSelector(SelectSelectorConfig(
                    options=["yes", "no"],
                    mode=SelectSelectorMode.LIST,
                    translation_key="paired",
                )),
            }),
        )

    async def async_step_pair_all(self, user_input=None):
        if user_input is not None:
            all_subentry = _get_all_subentry(self.config_entry)
            shutter_channel = self._existing(CONF_CHANNEL)
            all_channel = all_subentry.data[CONF_CHANNEL]
            await self._send(shutter_channel, CMD_PAIR_ALLOW)
            await asyncio.sleep(5)
            await self._send(all_channel, CMD_PAIR)
            return self.async_abort(reason="pair_all_successful")

        all_subentry = _get_all_subentry(self.config_entry)
        all_name = all_subentry.data.get(CONF_NAME, "All Shutters")
        shutter_name = self._existing(CONF_NAME, "?")

        return self.async_show_form(
            step_id="pair_all",
            data_schema=vol.Schema({}),
            description_placeholders={
                "shutter_name": shutter_name,
                "all_name": all_name,
            },
        )

    async def async_step_calibration_intro(self, user_input=None):
        if user_input is not None:
            if user_input.get("skip"):
                self._data[CONF_TRAVEL_TIME_UP] = self._existing(CONF_TRAVEL_TIME_UP, DEFAULT_TRAVEL_TIME)
                self._data[CONF_TRAVEL_TIME_DOWN] = self._existing(CONF_TRAVEL_TIME_DOWN, DEFAULT_TRAVEL_TIME)
                return self._finish_shutter()
            return await self.async_step_calibration_direction()

        return self.async_show_form(
            step_id="calibration_intro",
            data_schema=vol.Schema({
                vol.Optional("skip", default=False): bool,
            }),
        )

    async def async_step_calibration_direction(self, user_input=None):
        errors = {}

        if user_input is not None:
            direction = user_input.get("direction")
            if not direction:
                errors["base"] = "no_direction_selected"
            else:
                cmd = CMD_UP if direction == "up" else CMD_DOWN
                await self._send(self._data[CONF_CHANNEL], cmd)
                self._cal_start = time.monotonic()
                self._data["_cal_direction"] = direction
                return await self.async_step_calibration_wait()

        return self.async_show_form(
            step_id="calibration_direction",
            data_schema=vol.Schema({
                vol.Optional("direction"): vol.In(["up", "down"]),
            }),
            errors=errors,
        )

    async def async_step_calibration_wait(self, user_input=None):
        if user_input is not None:
            if self._cal_start is None:
                return self.async_abort(reason="unknown")
            elapsed = round(time.monotonic() - self._cal_start, 1)

            direction = self._data.pop("_cal_direction", "up")
            if direction == "up":
                self._data[CONF_TRAVEL_TIME_UP] = elapsed
                if CONF_TRAVEL_TIME_DOWN not in self._data:
                    await self._send(self._data[CONF_CHANNEL], CMD_DOWN)
                    self._cal_start = time.monotonic()
                    self._data["_cal_direction"] = "down"
                    return await self.async_step_calibration_wait()
            else:
                self._data[CONF_TRAVEL_TIME_DOWN] = elapsed
                if CONF_TRAVEL_TIME_UP not in self._data:
                    await self._send(self._data[CONF_CHANNEL], CMD_UP)
                    self._cal_start = time.monotonic()
                    self._data["_cal_direction"] = "up"
                    return await self.async_step_calibration_wait()

            last_direction = direction
            self._data[CONF_LAST_POSITION] = POSITION_OPEN if last_direction == "up" else POSITION_CLOSED
            return await self.async_step_calibration_done()

        direction = self._data.get("_cal_direction", "up")
        return self.async_show_form(
            step_id="calibration_wait",
            data_schema=vol.Schema({}),
            description_placeholders={
                "direction_arrow": "↑" if direction == "up" else "↓",
            },
        )

    async def async_step_calibration_done(self, user_input=None):
        if user_input is not None:
            return self._finish_shutter()

        return self.async_show_form(
            step_id="calibration_done",
            data_schema=vol.Schema({}),
            description_placeholders={
                "time_up": str(self._data.get(CONF_TRAVEL_TIME_UP, "?")),
                "time_down": str(self._data.get(CONF_TRAVEL_TIME_DOWN, "?")),
            },
        )

    async def async_step_signal_select_shutters(self, user_input=None):
        shutters = _get_shutter_subentries(self.config_entry)
        options = [
            {"value": s.subentry_id, "label": s.data.get(CONF_NAME, s.title)}
            for s in sorted(shutters, key=lambda s: s.data.get(CONF_NAME, s.title).lower())
        ]

        if user_input is not None:
            selected_ids = user_input.get("shutter_ids", [])
            self._signal_queue = []
            for sid in selected_ids:
                subentry = next((s for s in shutters if s.subentry_id == sid), None)
                if subentry is None:
                    continue
                name = subentry.data.get(CONF_NAME, subentry.title)
                self._signal_queue += [
                    {"key": CONF_SIGNAL_UP,   "shutter_id": sid, "label": f"UP — {name}"},
                    {"key": CONF_SIGNAL_STOP, "shutter_id": sid, "label": f"STOP — {name}"},
                    {"key": CONF_SIGNAL_DOWN, "shutter_id": sid, "label": f"DOWN — {name}"},
                ]
            if not self._signal_queue:
                return self.async_show_form(
                    step_id="signal_select_shutters",
                    data_schema=vol.Schema({
                        vol.Optional("shutter_ids", default=[]): SelectSelector(SelectSelectorConfig(
                            options=options,
                            multiple=True,
                            mode=SelectSelectorMode.LIST,
                        )),
                    }),
                    errors={"base": "no_shutter_selected"},
                )
            self._current_queue_index = 0
            self._learned = {}
            return await self.async_step_signal_wait()

        return self.async_show_form(
            step_id="signal_select_shutters",
            data_schema=vol.Schema({
                vol.Optional("shutter_ids", default=[]): SelectSelector(SelectSelectorConfig(
                    options=options,
                    multiple=True,
                    mode=SelectSelectorMode.LIST,
                )),
            }),
        )

    def _on_signal_received(self, raw: str) -> None:
        if len(raw) < 12:
            return
        if raw[10:12] not in ("01", "00", "02"):
            return
        self._last_signal = raw

    def _cleanup_signal_cb(self) -> None:
        if self._signal_cb_registered:
            usb = _get_usb(self.hass, self.config_entry.entry_id)
            if usb:
                usb.unregister_signal_callback(self._on_signal_received)
            self._signal_cb_registered = False

    async def async_step_signal_wait(self, user_input=None):
        if not self._signal_cb_registered:
            usb = _get_usb(self.hass, self.config_entry.entry_id)
            if usb:
                usb.register_signal_callback(self._on_signal_received)
                self._signal_cb_registered = True

        if user_input is not None:
            if self._last_signal is None:
                item = self._signal_queue[self._current_queue_index]
                return self.async_show_form(
                    step_id="signal_wait",
                    data_schema=vol.Schema({}),
                    errors={"base": "no_signal_received"},
                    description_placeholders={"label": item["label"]},
                )

            raw = self._last_signal
            self._last_signal = None
            pattern = raw[2:4] + raw[10:12]
            remote_id = raw[4:10]

            item = self._signal_queue[self._current_queue_index]
            sid = item["shutter_id"]
            self._learned.setdefault(sid, {})[item["key"]] = pattern
            if CONF_REMOTE_ID not in self._learned[sid]:
                self._learned[sid][CONF_REMOTE_ID] = remote_id

            self._current_queue_index += 1
            if self._current_queue_index < len(self._signal_queue):
                next_item = self._signal_queue[self._current_queue_index]
                return self.async_show_form(
                    step_id="signal_wait",
                    data_schema=vol.Schema({}),
                    description_placeholders={"label": next_item["label"]},
                )
            return await self.async_step_signal_summary()

        item = self._signal_queue[self._current_queue_index]
        return self.async_show_form(
            step_id="signal_wait",
            data_schema=vol.Schema({}),
            description_placeholders={"label": item["label"]},
        )

    async def async_step_signal_summary(self, user_input=None):
        if user_input is not None:
            return await self.async_step_signal_all_prompt()

        shutters = _get_shutter_subentries(self.config_entry)
        lines = []
        for sid, signals in self._learned.items():
            subentry = next((s for s in shutters if s.subentry_id == sid), None)
            name = subentry.data.get(CONF_NAME, subentry.title) if subentry else sid
            remote = signals.get(CONF_REMOTE_ID, "?")
            up   = signals.get(CONF_SIGNAL_UP,   "–")
            stop = signals.get(CONF_SIGNAL_STOP,  "–")
            down = signals.get(CONF_SIGNAL_DOWN,  "–")
            lines.append(f"{name} — UP: {up} | STOP: {stop} | DOWN: {down} (Remote: {remote})")
        summary = "\n".join(lines) if lines else "–"

        return self.async_show_form(
            step_id="signal_summary",
            data_schema=vol.Schema({}),
            description_placeholders={"summary": summary},
        )

    async def async_step_signal_all_prompt(self, user_input=None):
        if _get_all_subentry(self.config_entry) is None:
            return self._finish_signal_learning()

        if user_input is not None:
            if user_input.get("learn_all") == "yes":
                self._signal_queue = [
                    {"key": CONF_SIGNAL_ALL_UP,   "shutter_id": "__all__", "label": "ALL-UP"},
                    {"key": CONF_SIGNAL_ALL_STOP, "shutter_id": "__all__", "label": "ALL-STOP"},
                    {"key": CONF_SIGNAL_ALL_DOWN, "shutter_id": "__all__", "label": "ALL-DOWN"},
                ]
                self._current_queue_index = 0
                return await self.async_step_signal_wait()
            return self._finish_signal_learning()

        return self.async_show_form(
            step_id="signal_all_prompt",
            data_schema=vol.Schema({
                vol.Required("learn_all"): SelectSelector(SelectSelectorConfig(
                    options=["yes", "no"],
                    mode=SelectSelectorMode.LIST,
                    translation_key="learn_all",
                )),
            }),
        )

    def _finish_signal_learning(self):
        self._cleanup_signal_cb()
        shutters = _get_shutter_subentries(self.config_entry)

        for sid, signals in self._learned.items():
            if sid == "__all__":
                continue
            subentry = next((s for s in shutters if s.subentry_id == sid), None)
            if subentry is None:
                continue
            shutter_signals = {k: v for k, v in signals.items() if k != CONF_REMOTE_ID}
            self.hass.config_entries.async_update_subentry(
                self.config_entry,
                subentry,
                data={**subentry.data, **shutter_signals},
            )

        all_signals = self._learned.get("__all__", {})
        if all_signals:
            all_subentry = _get_all_subentry(self.config_entry)
            if all_subentry is not None:
                self.hass.config_entries.async_update_subentry(
                    self.config_entry,
                    all_subentry,
                    data={**all_subentry.data, **all_signals},
                )

        if self._is_reconfigure():
            return self.async_abort(reason="reconfigure_successful")
        return self._finish_remote()

    async def async_step_calibration_prompt(self, user_input=None):
        if user_input is not None:
            if user_input.get("calibrate_now") == "yes":
                return await self.async_step_calibration_intro()
            return self._finish_shutter()

        return self.async_show_form(
            step_id="calibration_prompt",
            data_schema=vol.Schema({
                vol.Required("calibrate_now"): SelectSelector(SelectSelectorConfig(
                    options=["yes", "no"],
                    mode=SelectSelectorMode.LIST,
                    translation_key="calibrate_now",
                )),
            }),
        )

    # --- Remote Flow ---

    async def async_step_remote_setup(self, user_input=None):
        if user_input is not None:
            self._data[CONF_NAME] = user_input[CONF_NAME]
            self._data["subentry_type"] = SUBENTRY_TYPE_REMOTE
            if self._is_reconfigure():
                return self._finish_remote()
            self._last_signal = None
            if not self._signal_cb_registered:
                usb = _get_usb(self.hass, self.config_entry.entry_id)
                if usb:
                    usb.register_signal_callback(self._on_signal_received)
                    self._signal_cb_registered = True
            return await self.async_step_remote_id_wait()

        existing_name = self._existing(CONF_NAME, "")
        return self.async_show_form(
            step_id="remote_setup",
            data_schema=vol.Schema({
                vol.Required(CONF_NAME, default=existing_name): str,
            }),
        )

    async def async_step_remote_id_wait(self, user_input=None):
        if user_input is not None:
            if self._last_signal is None:
                return self.async_show_form(
                    step_id="remote_id_wait",
                    data_schema=vol.Schema({}),
                    errors={"base": "no_signal_received"},
                )
            raw = self._last_signal
            self._last_signal = None
            self._data[CONF_REMOTE_ID] = raw[4:10]
            self._cleanup_signal_cb()
            return await self.async_step_remote_id_confirm()

        return self.async_show_form(
            step_id="remote_id_wait",
            data_schema=vol.Schema({}),
        )

    async def async_step_remote_id_confirm(self, user_input=None):
        if user_input is not None:
            return await self.async_step_signal_select_shutters()

        return self.async_show_form(
            step_id="remote_id_confirm",
            data_schema=vol.Schema({}),
            description_placeholders={
                "remote_id": self._data.get(CONF_REMOTE_ID, "?"),
            },
        )

    async def async_step_remote_reconfigure_menu(self, user_input=None):
        if user_input is not None:
            action = user_input.get("reconfigure_action")
            if action == "remote_name":
                return await self.async_step_remote_setup()
            if action == "remote_id":
                self._last_signal = None
                if not self._signal_cb_registered:
                    usb = _get_usb(self.hass, self.config_entry.entry_id)
                    if usb:
                        usb.register_signal_callback(self._on_signal_received)
                        self._signal_cb_registered = True
                return await self.async_step_remote_id_wait()
            return await self.async_step_signal_select_shutters()

        return self.async_show_form(
            step_id="remote_reconfigure_menu",
            data_schema=vol.Schema({
                vol.Required("reconfigure_action"): SelectSelector(SelectSelectorConfig(
                    options=["remote_name", "remote_id", "signals"],
                    mode=SelectSelectorMode.LIST,
                    translation_key="remote_reconfigure_action",
                )),
            }),
        )

    def _finish_remote(self):
        self._cleanup_signal_cb()
        name = self._data.get(CONF_NAME, "Remote")
        remote_id = self._data.get(CONF_REMOTE_ID, "")
        title = f"{name} ({remote_id})" if remote_id else name
        if self._is_reconfigure():
            self.hass.config_entries.async_update_subentry(
                self.config_entry,
                self._get_reconfigure_subentry(),
                title=title,
                data={**self._get_reconfigure_subentry().data, **self._data},
            )
            return self.async_abort(reason="reconfigure_successful")
        return self.async_create_entry(title=title, data=self._data)

    # --- Shutter Finish ---

    def _finish_shutter(self):
        self._cleanup_signal_cb()
        title = self._data.get(CONF_NAME, f"Shutter {self._data.get(CONF_CHANNEL, '?'):02X}")
        if self._is_reconfigure():
            self.hass.config_entries.async_update_subentry(
                self.config_entry,
                self._get_reconfigure_subentry(),
                title=title,
                data={**self._get_reconfigure_subentry().data, **self._data},
            )
            return self.async_abort(reason="reconfigure_successful")
        return self.async_create_entry(title=title, data=self._data)

    def _finish_all(self):
        title = self._data.get(CONF_NAME, "All Shutters")
        if self._is_reconfigure():
            self.hass.config_entries.async_update_subentry(
                self.config_entry,
                self._get_reconfigure_subentry(),
                title=title,
                data={**self._get_reconfigure_subentry().data, **self._data},
            )
            return self.async_abort(reason="reconfigure_successful")
        return self.async_create_entry(title=title, data=self._data)

    # --- All-Shutters Flow ---

    async def async_step_all_channel(self, user_input=None):
        errors = {}

        if user_input is not None:
            channel_raw = user_input.get(CONF_CHANNEL, "").strip().upper()
            if not channel_raw or len(channel_raw) != 2:
                errors["base"] = "invalid_channel"
            else:
                try:
                    channel = int(channel_raw, 16)
                except ValueError:
                    errors["base"] = "invalid_channel"
                else:
                    used = _get_used_channels(self.config_entry)
                    existing_channel = (
                        self._get_reconfigure_subentry().data.get(CONF_CHANNEL)
                        if self._is_reconfigure() else None
                    )
                    if channel in used and channel != existing_channel:
                        errors["base"] = "channel_in_use"
                    else:
                        self._data[CONF_CHANNEL] = channel
                        self._data[CONF_NAME] = user_input.get(CONF_NAME, "All Shutters")
                        self._data["subentry_type"] = SUBENTRY_TYPE_ALL
                        if user_input.get("already_paired") == "yes":
                            return self._finish_all()
                        self._shutter_list = _get_shutter_subentries(self.config_entry)
                        self._current_index = 0
                        return await self.async_step_pair_shutter()

        existing_name = (
            self._get_reconfigure_subentry().data.get(CONF_NAME, "")
            if self._is_reconfigure() else ""
        )
        existing_ch = (
            self._get_reconfigure_subentry().data.get(CONF_CHANNEL)
            if self._is_reconfigure() else None
        )
        ch_default = format(existing_ch, "02X") if existing_ch is not None else ""

        return self.async_show_form(
            step_id="all_channel",
            data_schema=vol.Schema({
                vol.Required(CONF_NAME, default=existing_name or "All Shutters"): str,
                vol.Required(CONF_CHANNEL, default=ch_default): str,
                vol.Required("already_paired"): SelectSelector(SelectSelectorConfig(
                    options=["yes", "no"],
                    mode=SelectSelectorMode.LIST,
                    translation_key="already_paired",
                )),
            }),
            errors=errors,
        )

    async def async_step_pair_shutter(self, user_input=None):
        if user_input is not None:
            shutter = self._shutter_list[self._current_index]
            shutter_channel = shutter.data[CONF_CHANNEL]
            all_channel = self._data[CONF_CHANNEL]

            await self._send(shutter_channel, CMD_PAIR_ALLOW)
            await asyncio.sleep(5)
            await self._send(all_channel, CMD_PAIR)

            self._current_index += 1
            if self._current_index < len(self._shutter_list):
                return await self.async_step_pair_shutter()

            return self._finish_all()

        shutter = self._shutter_list[self._current_index]
        shutter_name = shutter.data.get(CONF_NAME, shutter.title)
        total = len(self._shutter_list)
        current = self._current_index + 1

        return self.async_show_form(
            step_id="pair_shutter",
            data_schema=vol.Schema({}),
            description_placeholders={
                "shutter_name": shutter_name,
                "current": str(current),
                "total": str(total),
            },
        )

# ----------------------------------------------------------------------
# Main Config Flow
# ----------------------------------------------------------------------

class SchellenbergConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: config_entries.ConfigEntry
    ) -> dict[str, type[config_entries.ConfigSubentryFlow]]:
        return {
            SUBENTRY_TYPE_SHUTTER: SchellenbergShutterSubEntryFlow,
        }

    async def async_step_user(self, user_input=None):
        ports = await self.hass.async_add_executor_job(get_available_serial_ports)

        if not ports:
            return self.async_abort(reason="no_serial_ports")

        port_map = {p.label: p.device for p in ports}

        existing_titles = {
            entry.title
            for entry in self.hass.config_entries.async_entries(DOMAIN)
        }

        if user_input is not None:
            name = user_input[CONF_NAME].strip() or "Schellenberg USB-Stick"
            selected_label = user_input[CONF_SERIAL_PORT]
            stable_path = port_map.get(selected_label, selected_label)

            await self.async_set_unique_id(stable_path)
            self._abort_if_unique_id_configured()

            return self.async_create_entry(
                title=name,
                data={
                    CONF_SERIAL_PORT: stable_path,
                    CONF_SEND_REPEAT: int(user_input[CONF_SEND_REPEAT]),
                },
            )

        default_name = "Schellenberg USB-Stick"
        if default_name in existing_titles:
            suffix = 2
            while f"{default_name} {suffix}" in existing_titles:
                suffix += 1
            default_name = f"{default_name} {suffix}"

        schema = vol.Schema({
            vol.Required(CONF_NAME, default=default_name): str,
            vol.Required(CONF_SERIAL_PORT): vol.In(list(port_map.keys())),
            vol.Required(CONF_SEND_REPEAT, default=DEFAULT_SEND_REPEAT): vol.All(
                int, vol.Range(min=MIN_SEND_REPEAT, max=MAX_SEND_REPEAT)
            ),
        })

        return self.async_show_form(step_id="user", data_schema=schema)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return SchellenbergOptionsFlow()