"""
fi_host.transport.serial_driver
Async-capable serial driver for the ESP32-S3 rig.
Handles USB-CDC (ESP32-S3 native USB) and UART bridges (CH340/CP210x).
"""
from __future__ import annotations
import asyncio
import json
import threading
import time
from typing import AsyncIterator, Iterator, Optional

import serial
import serial.tools.list_ports

from fi_host.core import GlitchParams, SweepParams, GlitchRecord, SessionStats

DEFAULT_BAUD    = 921600
RESP_TIMEOUT    = 5.0


class RigConnectionError(Exception): ...
class RigTimeoutError(Exception): ...


class RigSerial:
    """
    Synchronous serial driver, thread-safe.
    Handles both USB-CDC (ESP32-S3 native) and UART-USB bridge chips.
    """

    def __init__(self, port: str, baud: int = DEFAULT_BAUD):
        self.port  = port
        self.baud  = baud
        self._ser  = None
        self._lock = threading.Lock()
        self.stats = SessionStats()

    def connect(self) -> bool:
        try:
            self._ser = serial.Serial(
                self.port, self.baud,
                timeout       = RESP_TIMEOUT,
                write_timeout = 2.0,
                # Keep DTR/RTS low so opening the port does NOT
                # reset the ESP32 (important for USB-CDC)
                dsrdtr  = False,
                rtscts  = False,
                xonxoff = False,
            )

            # USB-CDC needs more settle time than UART bridges
            time.sleep(0.8)
            self._ser.reset_input_buffer()
            self._ser.reset_output_buffer()

            # Retry STATUS up to 5 times with growing delays.
            # The ESP32-S3 USB-CDC stack can take 1-2s to fully enumerate
            # and the boot banner appears before our STATUS response.
            for attempt in range(5):
                try:
                    self._write("STATUS")
                    # Read up to 15 lines - boot messages arrive before STATUS
                    for _ in range(15):
                        line = self._readline()
                        if not line:
                            break
                        try:
                            resp = json.loads(line)
                            # Accept both the boot banner and the status reply
                            if resp.get("status") in ("ready", "boot_ok"):
                                return True
                        except json.JSONDecodeError:
                            continue  # skip non-JSON boot log lines
                except Exception:
                    pass

                wait = 0.5 * (attempt + 1)
                time.sleep(wait)
                self._ser.reset_input_buffer()

            return False

        except serial.SerialException as e:
            raise RigConnectionError(f"Could not open {self.port}: {e}") from e

    def disconnect(self):
        if self._ser and self._ser.is_open:
            self._ser.close()

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *_):
        self.disconnect()

    def _write(self, cmd: str):
        self._ser.write((cmd + "\n").encode())
        self._ser.flush()

    def _readline(self) -> Optional[str]:
        try:
            raw = self._ser.readline()
            return raw.decode("utf-8", errors="replace").strip() or None
        except serial.SerialTimeoutException:
            return None

    def _cmd(self, cmd: str) -> Optional[dict]:
        with self._lock:
            self._write(cmd)
            # Read lines until we get a JSON response (skip log lines)
            for _ in range(15):
                line = self._readline()
                if not line:
                    return None
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    continue
            return None

    def reset_target(self) -> bool:
        resp = self._cmd("RESET")
        return resp is not None and resp.get("status") == "reset_ok"

    def firmware_status(self) -> Optional[dict]:
        return self._cmd("STATUS")

    def glitch_once(self, params: GlitchParams) -> Optional[GlitchRecord]:
        with self._lock:
            self._write(params.to_firmware_cmd())
            for _ in range(15):
                line = self._readline()
                if not line:
                    return None
                rec = GlitchRecord.from_json_line(line)
                if rec:
                    self.stats.record(rec)
                    return rec
        return None

    def sweep_iter(self, params: SweepParams) -> Iterator[GlitchRecord]:
        total = params.total_combinations
        with self._lock:
            self._write(params.to_firmware_cmd())
            received = 0
            while received < total:
                line = self._readline()
                if line is None:
                    break
                if '"sweep_done"' in line:
                    break
                rec = GlitchRecord.from_json_line(line)
                if rec:
                    rec.attempt = received
                    received   += 1
                    self.stats.record(rec)
                    yield rec

    @staticmethod
    def list_ports() -> list[dict]:
        return [
            {"device": p.device, "description": p.description, "hwid": p.hwid}
            for p in serial.tools.list_ports.comports()
        ]


class AsyncRigSerial:
    """
    Async wrapper - offloads blocking serial I/O to a thread pool.
    Safe for use in FastAPI / asyncio contexts.
    """

    def __init__(self, port: str, baud: int = DEFAULT_BAUD):
        self._sync = RigSerial(port, baud)

    async def connect(self) -> bool:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._sync.connect)

    async def disconnect(self):
        self._sync.disconnect()

    async def firmware_status(self) -> Optional[dict]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._sync.firmware_status)

    async def reset_target(self) -> bool:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._sync.reset_target)

    async def glitch_once(self, params: GlitchParams) -> Optional[GlitchRecord]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._sync.glitch_once, params)

    async def sweep_stream(self, params: SweepParams) -> AsyncIterator[GlitchRecord]:
        loop  = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()

        def _blocking():
            try:
                for rec in self._sync.sweep_iter(params):
                    asyncio.run_coroutine_threadsafe(queue.put(rec), loop)
            finally:
                asyncio.run_coroutine_threadsafe(queue.put(None), loop)

        thread = threading.Thread(target=_blocking, daemon=True)
        thread.start()

        while True:
            rec = await queue.get()
            if rec is None:
                break
            yield rec

    @property
    def stats(self) -> SessionStats:
        return self._sync.stats

    @staticmethod
    def list_ports() -> list[dict]:
        return RigSerial.list_ports()
