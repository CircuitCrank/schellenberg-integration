import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass

import serial_asyncio
from serial.tools import list_ports

from .const import (
    DEFAULT_BAUD_RATE,
    COMMAND_TIMEOUT,
    USB_PREFIX,
    USB_MODE_INITIAL,
    USB_MODE_LISTENING,
    ACK_TX_ON,
    ACK_TX_OFF,
    ACK_TX_ERROR,
    MIN_SEND_REPEAT,
    MAX_SEND_REPEAT,
)

_LOGGER = logging.getLogger(__name__)

RECONNECT_INTERVAL = 10
ECHO_SUPPRESS_SECONDS = 0.5
MIN_SIGNAL_LENGTH = 12

SignalCallback = Callable[[str], None]
StateCallback = Callable[[], None]


@dataclass
class SerialPortInfo:
    device: str
    description: str
    vid: int | None
    pid: int | None

    @property
    def label(self) -> str:
        return f"{self.description} — {self.device}"


def get_available_serial_ports() -> list[SerialPortInfo]:
    result = []
    for port in list_ports.comports():
        if not port.device:
            continue
        stable_path = _resolve_stable_path(port.device)
        result.append(SerialPortInfo(
            device=stable_path,
            description=port.description or port.device,
            vid=port.vid,
            pid=port.pid,
        ))
    return result


def _resolve_stable_path(device: str) -> str:
    import os
    by_id_dir = "/dev/serial/by-id"
    try:
        if os.path.isdir(by_id_dir):
            for name in os.listdir(by_id_dir):
                full = os.path.join(by_id_dir, name)
                if os.path.realpath(full) == os.path.realpath(device):
                    return full
    except OSError:
        pass
    return device


class SchellenbergUSB:
    def __init__(self, port: str, baud_rate: int = DEFAULT_BAUD_RATE) -> None:
        self._port = port
        self._baud_rate = baud_rate
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._write_lock = asyncio.Lock()
        self._connected = False
        self._shutting_down = False
        self._reader_task: asyncio.Task | None = None
        self._reconnect_task: asyncio.Task | None = None
        self._signal_callbacks: list[SignalCallback] = []
        self._raw_callbacks: list[SignalCallback] = []
        self._disconnect_callbacks: list[StateCallback] = []
        self._reconnect_callbacks: list[StateCallback] = []
        self._ack_future: asyncio.Future[bool] | None = None
        self._ack_token: int = 0
        self._echo_suppress: set[str] = set()

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    async def connect(self) -> bool:
        self._resolve_pending_ack(False)
        self._echo_suppress.clear()
        try:
            self._reader, self._writer = await serial_asyncio.open_serial_connection(
                url=self._port,
                baudrate=self._baud_rate,
            )
            await self._init_stick()
            self._connected = True
            self._reader_task = asyncio.create_task(self._reader_loop())
            _LOGGER.debug("Connected to %s", self._port)
            return True
        except Exception as exc:
            _LOGGER.error("Failed to connect to %s: %s", self._port, exc)
            return False

    async def disconnect(self) -> None:
        self._shutting_down = True
        self._connected = False
        self._resolve_pending_ack(False)

        if self._reconnect_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()
            try:
                await self._reconnect_task
            except asyncio.CancelledError:
                pass

        await self._cancel_reader_task()

        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass

    async def _init_stick(self) -> None:
        for mode in (USB_MODE_INITIAL, USB_MODE_LISTENING):
            self._writer.write(f"{mode}\r\n".encode())
            await self._writer.drain()
            await asyncio.sleep(0.1)

    # ------------------------------------------------------------------
    # Callback registration
    # ------------------------------------------------------------------

    def register_signal_callback(self, callback: SignalCallback) -> None:
        if callback not in self._signal_callbacks:
            self._signal_callbacks.append(callback)

    def unregister_signal_callback(self, callback: SignalCallback) -> None:
        try:
            self._signal_callbacks.remove(callback)
        except ValueError:
            pass

    def register_raw_callback(self, callback: SignalCallback) -> None:
        if callback not in self._raw_callbacks:
            self._raw_callbacks.append(callback)

    def unregister_raw_callback(self, callback: SignalCallback) -> None:
        try:
            self._raw_callbacks.remove(callback)
        except ValueError:
            pass

    def register_disconnect_callback(self, callback: StateCallback) -> None:
        if callback not in self._disconnect_callbacks:
            self._disconnect_callbacks.append(callback)

    def unregister_disconnect_callback(self, callback: StateCallback) -> None:
        try:
            self._disconnect_callbacks.remove(callback)
        except ValueError:
            pass

    def register_reconnect_callback(self, callback: StateCallback) -> None:
        if callback not in self._reconnect_callbacks:
            self._reconnect_callbacks.append(callback)

    def unregister_reconnect_callback(self, callback: StateCallback) -> None:
        try:
            self._reconnect_callbacks.remove(callback)
        except ValueError:
            pass

    # ------------------------------------------------------------------
    # Reader loop
    # ------------------------------------------------------------------

    async def _reader_loop(self) -> None:
        while self._connected:
            try:
                raw = await self._reader.readline()
                if not raw:
                    raise EOFError("Serial port closed")
                line = raw.decode(errors="replace").strip()
                if not line:
                    continue
                self._dispatch(line)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                if self._shutting_down:
                    break
                _LOGGER.warning("USB disconnected from %s: %s", self._port, exc)
                self._connected = False
                self._resolve_pending_ack(False)
                self._fire_callbacks(self._disconnect_callbacks)
                self._reconnect_task = asyncio.create_task(self._reconnect_loop())
                break

    async def _cancel_reader_task(self) -> None:
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass

    async def _reconnect_loop(self) -> None:
        while not self._shutting_down:
            await asyncio.sleep(RECONNECT_INTERVAL)
            if self._shutting_down:
                break
            _LOGGER.debug("Attempting reconnect to %s", self._port)

            await self._cancel_reader_task()

            if await self.connect():
                _LOGGER.warning("Reconnected to %s", self._port)
                self._fire_callbacks(self._reconnect_callbacks)
                return
            _LOGGER.warning("Reconnect failed, retrying in %ds", RECONNECT_INTERVAL)

    def _fire_callbacks(self, callbacks: list[StateCallback]) -> None:
        for cb in callbacks:
            try:
                cb()
            except Exception as exc:
                _LOGGER.warning("Callback error: %s", exc)

    def _resolve_pending_ack(self, result: bool, token: int | None = None) -> None:
        if self._ack_future is not None and not self._ack_future.done():
            if token is None or token == self._ack_token:
                self._ack_future.set_result(result)

    def _dispatch(self, line: str) -> None:
        for cb in self._raw_callbacks:
            try:
                cb(line)
            except Exception as exc:
                _LOGGER.warning("Raw callback error: %s", exc)

        if line in (ACK_TX_ON, ACK_TX_OFF, ACK_TX_ERROR):
            if line != ACK_TX_ON:
                self._resolve_pending_ack(line == ACK_TX_OFF, self._ack_token)
            return

        if not line.startswith(USB_PREFIX):
            return

        if len(line) < MIN_SIGNAL_LENGTH:
            _LOGGER.debug("Dropping short frame: %r", line)
            return

        if line in self._echo_suppress:
            _LOGGER.debug("Suppressing echo: %r", line)
            return

        for cb in self._signal_callbacks:
            try:
                cb(line)
            except Exception as exc:
                _LOGGER.warning("Signal callback error: %s", exc)

    # ------------------------------------------------------------------
    # Command sending
    # ------------------------------------------------------------------

    async def send_command(self, enumerator: int, command: int, repeat: int) -> bool:
        repeat = max(MIN_SEND_REPEAT, min(MAX_SEND_REPEAT, repeat))

        if not self._connected:
            _LOGGER.warning("Cannot send command: not connected")
            return False

        cmd_str = f"{USB_PREFIX}{enumerator:02X}{repeat:X}{command:02X}0000\r\n"
        signal_frame = cmd_str.strip()

        async with self._write_lock:
            if not self._connected or self._writer is None:
                return False
            self._ack_token += 1
            current_token = self._ack_token
            loop = asyncio.get_running_loop()
            self._ack_future = loop.create_future()
            try:
                self._echo_suppress.add(signal_frame)
                loop.call_later(
                    ECHO_SUPPRESS_SECONDS,
                    self._echo_suppress.discard,
                    signal_frame,
                )
                self._writer.write(cmd_str.encode())
                await self._writer.drain()
                async with asyncio.timeout(COMMAND_TIMEOUT):
                    result = await self._ack_future
                if not self._connected:
                    return False
                return result
            except asyncio.TimeoutError:
                _LOGGER.error("ACK timeout for command 0x%02X on channel %02X", command, enumerator)
                return False
            except Exception as exc:
                _LOGGER.error("Send failed: %s", exc)
                return False
            finally:
                self._ack_future = None

    @property
    def connected(self) -> bool:
        return self._connected