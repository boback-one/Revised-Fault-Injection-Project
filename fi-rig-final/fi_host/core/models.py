"""
fi_host.core.models — shared data models (Pydantic v2).
Used by transport, analysis, CLI, and API layers.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from enum import IntEnum
from typing import Optional
from pydantic import BaseModel, Field


# ─────────────────────────── ENUMS ──────────────────────────────────────────

class GlitchResult(IntEnum):
    OK      = 0
    FAULT   = 1
    CRASH   = 2
    TIMEOUT = 3


RESULT_LABEL  = {0: "OK", 1: "FAULT", 2: "CRASH", 3: "TIMEOUT"}
RESULT_COLOR  = {0: "green", 1: "red", 2: "yellow", 3: "magenta"}

RAIL_NAMES = {
    0: "VCC_CORE",
    1: "VCC_IO",
    2: "VCC_PLL",
    3: "VCC_FLASH",
    4: "VCC_ADC",
    5: "VCC_RTC",
    6: "VCC_USB",
    7: "VCC_AUX",
}


# ─────────────────────────── PARAMS ─────────────────────────────────────────

class GlitchParams(BaseModel):
    trigger_delay_ns:  int = Field(1000,  ge=10,      le=1_000_000)
    glitch_width_ns:   int = Field(100,   ge=5,       le=50_000)
    target_rail:       int = Field(0,     ge=0,       le=7)
    repeat:            int = Field(1,     ge=1,       le=10_000)
    capture_window_ns: int = Field(10_000,ge=1_000,   le=1_000_000)
    expect_byte:       int = Field(0xFF,  ge=0,       le=0xFF)

    @property
    def rail_name(self) -> str:
        return RAIL_NAMES.get(self.target_rail, f"RAIL_{self.target_rail}")

    def to_firmware_cmd(self) -> str:
        return (f"GLITCH {self.trigger_delay_ns} {self.glitch_width_ns} "
                f"{self.target_rail} {self.repeat} "
                f"{self.capture_window_ns} {self.expect_byte:02X}")


class SweepParams(BaseModel):
    delay_start_ns: int = Field(500,    ge=10)
    delay_end_ns:   int = Field(25_000, ge=100)
    delay_step_ns:  int = Field(500,    ge=10)
    width_start_ns: int = Field(20,     ge=5)
    width_end_ns:   int = Field(2_000,  ge=10)
    width_step_ns:  int = Field(50,     ge=5)
    target_rail:    int = Field(0,      ge=0, le=7)
    capture_window_ns: int = Field(10_000)
    expect_byte:    int = Field(0xFF)

    @property
    def total_combinations(self) -> int:
        d = max(1, (self.delay_end_ns - self.delay_start_ns) // self.delay_step_ns + 1)
        w = max(1, (self.width_end_ns - self.width_start_ns) // self.width_step_ns + 1)
        return d * w

    def to_firmware_cmd(self) -> str:
        return (f"SWEEP {self.delay_start_ns} {self.delay_end_ns} {self.delay_step_ns} "
                f"{self.width_start_ns} {self.width_end_ns} {self.width_step_ns} "
                f"{self.target_rail}")


# ─────────────────────────── RECORD ─────────────────────────────────────────

class GlitchRecord(BaseModel):
    trigger_delay_ns: int
    glitch_width_ns:  int
    target_rail:      int
    result:           GlitchResult
    adc_min_mv:       int = 0
    clock_edges:      int = 0
    response_byte:    str = "0x00"
    attempt:          int = 0
    timestamp:        str = Field(default_factory=lambda: datetime.utcnow().isoformat())

    @property
    def is_fault(self)   -> bool: return self.result == GlitchResult.FAULT
    @property
    def is_crash(self)   -> bool: return self.result == GlitchResult.CRASH
    @property
    def is_ok(self)      -> bool: return self.result == GlitchResult.OK
    @property
    def rail_name(self)  -> str:  return RAIL_NAMES.get(self.target_rail, f"RAIL_{self.target_rail}")
    @property
    def result_label(self) -> str: return RESULT_LABEL.get(int(self.result), "?")

    @classmethod
    def from_json_line(cls, line: str) -> Optional["GlitchRecord"]:
        import json
        try:
            d = json.loads(line.strip())
            if "res" not in d and "result" not in d:
                return None
            return cls(
                trigger_delay_ns = d.get("d",  d.get("trigger_delay_ns", 0)),
                glitch_width_ns  = d.get("w",  d.get("glitch_width_ns",  0)),
                target_rail      = d.get("r",  d.get("target_rail",      0)),
                result           = GlitchResult(d.get("res", d.get("result", 0))),
                adc_min_mv       = d.get("mv", d.get("adc_min_mv",       0)),
                clock_edges      = d.get("clk",d.get("clock_edges",      0)),
                response_byte    = d.get("byte",d.get("response_byte", "0x00")),
            )
        except Exception:
            return None


# ─────────────────────────── SESSION ────────────────────────────────────────

class SessionStats(BaseModel):
    total:   int = 0
    ok:      int = 0
    fault:   int = 0
    crash:   int = 0
    timeout: int = 0

    def record(self, r: GlitchRecord):
        self.total += 1
        if   r.result == GlitchResult.OK:      self.ok      += 1
        elif r.result == GlitchResult.FAULT:   self.fault   += 1
        elif r.result == GlitchResult.CRASH:   self.crash   += 1
        elif r.result == GlitchResult.TIMEOUT: self.timeout += 1

    @property
    def fault_rate(self) -> float:
        return (self.fault / self.total * 100) if self.total else 0.0

    @property
    def crash_rate(self) -> float:
        return (self.crash / self.total * 100) if self.total else 0.0
