#!/usr/bin/env python3
"""Feedback decoding and small-step closed-loop control for Tracer + Piper.

This module is deliberately offline and stdlib-only: it decodes CAN payloads
that are already captured, and turns them into bounded setpoints. It never
opens a bus, a socket, a camera or a robot SDK.

Frame layouts come from the 2026-11-24 passive audit
(``AgileX-实验记录/feedback-hardware-audit.md`` and ``logs/``):

* ``0x311`` odometry: ``int32 left_wheel, int32 right_wheel`` (big-endian,
  cumulative counts).  Static sample ``00000228 00000296`` = 552 / 662.
* ``0x251`` / ``0x252`` actuator high-speed state: ``int16 rpm,
  int16 current*0.1 A, int32 pulse_count``.  Static samples decode to
  1.2 A / -204503 and 0.4 A / 170784.
* ``0x2A1`` Piper arm status: ``u8 ctrl_mode, u8 arm_status, u8 mode_feed,
  u8 teach_status, u8 motion_status, u8 trajectory_num, u16 err_code``.
* ``0x2A5`` / ``0x2A6`` / ``0x2A7``: two ``int32`` joint angles each,
  big-endian, unit 0.001 degree, 200 Hz on the arm buses.

Two rules are enforced in code rather than documented and forgotten:

1. Wheel counts never become metres without an explicit
   :class:`WheelCalibration` (``counts_per_meter`` and ``track_width_m``).
   The default is ``None`` and the conversion returns ``distance_m=None``
   with ``UNCALIBRATED``.
2. The chassis loop only closes on a source that passes the quality gate
   (fresh, calibrated, not constant-zero while commanded, no jump, in range).
   Anything else commands exactly zero with the rejection code.

CAN identifiers are bus-scoped.  ``0x311`` / ``0x251`` / ``0x252`` are
Tracer frames from ``can0``; ``0x2A1`` / ``0x2A5`` / ``0x2A6`` / ``0x2A7`` are
Piper frames from ``can_left`` or ``can_right``.  The Piper buses reuse
``0x251``-``0x256`` for arm high-speed feedback with a different payload, so a
frame without its interface is rejected rather than guessed.

The module also refuses samples that mix clock domains (default
``"monotonic"``; ``candump -L`` frames are ``"epoch"``), samples whose
interval exceeds ``max_dt_s``, and pulse pairs without an explicit
:class:`PulseMapping`.  Int32 wheel counters are unwrapped at 2**31.
Arm feedback must come with an external enable state and lag (or a current
setpoint); the assembler never fills in ``True``/``0.0``.
"""
from __future__ import annotations

import math
import re
import struct
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# CAN identifiers
# ---------------------------------------------------------------------------
CAN_ID_ODOMETRY = 0x311
CAN_ID_ACTUATOR1_HS = 0x251
CAN_ID_ACTUATOR2_HS = 0x252
CAN_ID_ARM_STATUS = 0x2A1
CAN_ID_ARM_JOINT_12 = 0x2A5
CAN_ID_ARM_JOINT_34 = 0x2A6
CAN_ID_ARM_JOINT_56 = 0x2A7

FRAME_LEN = 8

CHASSIS_BUS = "can0"
ARM_BUSES = frozenset({"can_left", "can_right"})

SOURCE_WHEEL = "wheel_0x311"
SOURCE_ACTUATOR = "actuator_pulse"

CLOCK_MONOTONIC = "monotonic"
CLOCK_EPOCH = "epoch"

_INT32_HALF = 1 << 31
_INT32_SPAN = 1 << 32


def int32_delta(current: int, previous: int) -> int:
    """Difference two int32 counters across the 2**31 wrap point."""
    delta = int(current) - int(previous)
    if delta > _INT32_HALF:
        delta -= _INT32_SPAN
    elif delta < -_INT32_HALF:
        delta += _INT32_SPAN
    return delta


# ---------------------------------------------------------------------------
# Frame errors and raw frame
# ---------------------------------------------------------------------------


class FrameError(ValueError):
    """A frame is malformed or carries an impossible payload."""


class UnsupportedFrame(FrameError):
    """A CAN identifier that this module does not consume as feedback."""


class FrameBusError(FrameError):
    """A frame arrived on the wrong CAN interface for its identifier."""


@dataclass(frozen=True)
class CanFrame:
    can_id: int
    data: bytes
    timestamp: Optional[float] = None
    iface: Optional[str] = None
    clock: Optional[str] = None


def _require_bus(can_id: int, iface: Optional[str]) -> None:
    if iface is None:
        raise FrameBusError("0x%03X needs an explicit CAN interface" % can_id)
    if can_id in (CAN_ID_ODOMETRY, CAN_ID_ACTUATOR1_HS, CAN_ID_ACTUATOR2_HS):
        if iface != CHASSIS_BUS:
            raise FrameBusError(
                "0x%03X belongs to %s, got %r" % (can_id, CHASSIS_BUS, iface)
            )
        return
    if can_id in (
        CAN_ID_ARM_STATUS,
        CAN_ID_ARM_JOINT_12,
        CAN_ID_ARM_JOINT_34,
        CAN_ID_ARM_JOINT_56,
    ):
        if iface not in ARM_BUSES:
            raise FrameBusError(
                "0x%03X belongs to %s, got %r"
                % (can_id, sorted(ARM_BUSES), iface)
            )
        return
    raise UnsupportedFrame("0x%03X has no bus assignment" % can_id)


_CANDUMP = re.compile(
    r"^\s*(?:\((?P<ts>[0-9]+(?:\.[0-9]+)?)\)\s+)?"
    r"(?P<iface>[A-Za-z0-9_.-]+)\s+"
    r"(?P<can_id>[0-9A-Fa-f]+)#(?P<data>[0-9A-Fa-f]*)\s*$"
)


def parse_candump_line(line: str) -> CanFrame:
    """Parse one ``candump -L`` line, e.g. ``(ts) can0 311#0000022800000296``."""
    if not isinstance(line, str):
        raise TypeError("candump line must be a string")
    match = _CANDUMP.match(line)
    if match is None:
        raise FrameError("not a candump line: %r" % (line,))
    can_id = int(match.group("can_id"), 16)
    payload_hex = match.group("data")
    if len(payload_hex) != FRAME_LEN * 2:
        raise FrameError(
            "payload must be %d bytes, got %d hex chars"
            % (FRAME_LEN, len(payload_hex))
        )
    timestamp = match.group("ts")
    timestamp_value = float(timestamp) if timestamp is not None else None
    return CanFrame(
        can_id=can_id,
        data=bytes.fromhex(payload_hex),
        timestamp=timestamp_value,
        iface=match.group("iface"),
        clock=CLOCK_EPOCH if timestamp_value is not None else None,
    )


def _payload(data, can_id: int) -> bytes:
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise TypeError("payload must be bytes-like, got %s" % type(data).__name__)
    raw = bytes(data)
    if len(raw) != FRAME_LEN:
        raise FrameError(
            "0x%03X payload must be %d bytes, got %d" % (can_id, FRAME_LEN, len(raw))
        )
    return raw


# ---------------------------------------------------------------------------
# Decoded payloads
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WheelCounts:
    """Cumulative wheel counts from ``0x311`` (or paired ``0x251/0x252``)."""

    timestamp: Optional[float]
    left: int
    right: int
    source: str = SOURCE_WHEEL
    can_id: int = CAN_ID_ODOMETRY
    iface: Optional[str] = None
    clock: Optional[str] = None
    pulse_mapping: Optional["PulseMapping"] = None


@dataclass(frozen=True)
class ActuatorHighSpeed:
    """``0x251`` / ``0x252`` high-speed actuator state."""

    timestamp: Optional[float]
    rpm: int
    current_a: float
    pulse_count: int
    can_id: int = CAN_ID_ACTUATOR1_HS
    iface: Optional[str] = None
    clock: Optional[str] = None


@dataclass(frozen=True)
class ArmJointPair:
    """Two joint angles from ``0x2A5`` / ``0x2A6`` / ``0x2A7``."""

    timestamp: Optional[float]
    can_id: int
    first_joint: int
    joints_deg: Tuple[float, float]
    iface: Optional[str] = None
    clock: Optional[str] = None


_ARM_STATUS_FAULTS = frozenset(
    {0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08, 0x09, 0x0A, 0x0E, 0x0F, 0xFF}
)
_ARM_MODE_NAMES = {
    0x00: "MOVE_P",
    0x01: "MOVE_J",
    0x02: "MOVE_L",
    0x03: "MOVE_C",
    0x04: "MOVE_M",
    0x05: "MOVE_CPV",
}
_ARM_CTRL_NAMES = {
    0x00: "STANDBY",
    0x01: "CAN_CTRL",
    0x02: "TEACHING",
    0x03: "ETHERNET",
    0x04: "WIFI",
    0x05: "REMOTE",
    0x06: "LINKAGE_TEACHING",
    0x07: "OFFLINE_TRAJECTORY",
}


@dataclass(frozen=True)
class ArmStatusFeedback:
    """Decoded ``0x2A1`` arm status."""

    timestamp: Optional[float]
    ctrl_mode: int
    arm_status: int
    mode_feed: int
    teach_status: int
    motion_status: int
    trajectory_num: int
    error_code: int
    iface: Optional[str] = None
    clock: Optional[str] = None

    @property
    def mode_name(self) -> str:
        return _ARM_MODE_NAMES.get(self.mode_feed, "UNKNOWN")

    @property
    def ctrl_mode_name(self) -> str:
        return _ARM_CTRL_NAMES.get(self.ctrl_mode, "UNKNOWN")

    @property
    def fault(self) -> Optional[str]:
        if self.arm_status in _ARM_STATUS_FAULTS or self.error_code:
            return "arm_status=0x%02X err_code=0x%04X" % (
                self.arm_status,
                self.error_code,
            )
        return None


def parse_wheel_counts(
    data,
    timestamp: Optional[float] = None,
    iface: Optional[str] = None,
    clock: Optional[str] = None,
) -> WheelCounts:
    _require_bus(CAN_ID_ODOMETRY, iface)
    raw = _payload(data, CAN_ID_ODOMETRY)
    left, right = struct.unpack(">ii", raw)
    return WheelCounts(
        timestamp=timestamp, left=left, right=right, iface=iface, clock=clock
    )


def parse_actuator_hs(
    data,
    can_id: int = CAN_ID_ACTUATOR1_HS,
    timestamp: Optional[float] = None,
    iface: Optional[str] = None,
    clock: Optional[str] = None,
) -> ActuatorHighSpeed:
    if can_id not in (CAN_ID_ACTUATOR1_HS, CAN_ID_ACTUATOR2_HS):
        raise UnsupportedFrame("0x%03X is not an actuator high-speed state" % can_id)
    _require_bus(can_id, iface)
    raw = _payload(data, can_id)
    rpm, current_raw, pulse_count = struct.unpack(">hhi", raw)
    return ActuatorHighSpeed(
        timestamp=timestamp,
        rpm=rpm,
        current_a=current_raw / 10.0,
        pulse_count=pulse_count,
        can_id=can_id,
        iface=iface,
        clock=clock,
    )


def parse_arm_status(
    data,
    timestamp: Optional[float] = None,
    iface: Optional[str] = None,
    clock: Optional[str] = None,
) -> ArmStatusFeedback:
    _require_bus(CAN_ID_ARM_STATUS, iface)
    raw = _payload(data, CAN_ID_ARM_STATUS)
    return ArmStatusFeedback(
        timestamp=timestamp,
        ctrl_mode=raw[0],
        arm_status=raw[1],
        mode_feed=raw[2],
        teach_status=raw[3],
        motion_status=raw[4],
        trajectory_num=raw[5],
        error_code=(raw[6] << 8) | raw[7],
        iface=iface,
        clock=clock,
    )


_ARM_JOINT_FIRST = {
    CAN_ID_ARM_JOINT_12: 1,
    CAN_ID_ARM_JOINT_34: 3,
    CAN_ID_ARM_JOINT_56: 5,
}


def parse_arm_joints(
    can_id: int,
    data,
    timestamp: Optional[float] = None,
    iface: Optional[str] = None,
    clock: Optional[str] = None,
) -> ArmJointPair:
    first = _ARM_JOINT_FIRST.get(can_id)
    if first is None:
        raise UnsupportedFrame("0x%03X is not an arm joint frame" % can_id)
    _require_bus(can_id, iface)
    raw = _payload(data, can_id)
    a, b = struct.unpack(">ii", raw)
    return ArmJointPair(
        timestamp=timestamp,
        can_id=can_id,
        first_joint=first,
        joints_deg=(a / 1000.0, b / 1000.0),
        iface=iface,
        clock=clock,
    )


def decode_frame(
    can_id: int,
    data,
    timestamp: Optional[float] = None,
    iface: Optional[str] = None,
    clock: Optional[str] = None,
):
    """Decode one 8-byte feedback frame into its typed payload.

    ``iface`` is mandatory for every supported identifier because CAN IDs are
    bus-scoped (chassis ``can0`` vs Piper ``can_left``/``can_right``).
    """
    if can_id == CAN_ID_ODOMETRY:
        return parse_wheel_counts(data, timestamp, iface, clock)
    if can_id in (CAN_ID_ACTUATOR1_HS, CAN_ID_ACTUATOR2_HS):
        return parse_actuator_hs(data, can_id, timestamp, iface, clock)
    if can_id == CAN_ID_ARM_STATUS:
        return parse_arm_status(data, timestamp, iface, clock)
    if can_id in _ARM_JOINT_FIRST:
        return parse_arm_joints(can_id, data, timestamp, iface, clock)
    raise UnsupportedFrame(
        "0x%03X is not a feedback source in this module" % can_id
    )


@dataclass(frozen=True)
class PulseMapping:
    """Explicit left/right assignment for the two motor pulse CAN IDs.

    The audit only established that ``0x251``/``0x252`` are actuator 1/2; it
    did not establish which wheel is left.  A motion test must set this before
    pulse feedback can be paired.
    """

    left_can_id: int
    right_can_id: int

    def __post_init__(self) -> None:
        if self.left_can_id == self.right_can_id:
            raise ValueError("pulse mapping needs two distinct CAN IDs")
        if {self.left_can_id, self.right_can_id} != {
            CAN_ID_ACTUATOR1_HS,
            CAN_ID_ACTUATOR2_HS,
        }:
            raise ValueError("pulse mapping must use 0x251 and 0x252")


def pair_actuator_pulse(
    first: ActuatorHighSpeed,
    second: ActuatorHighSpeed,
    mapping: PulseMapping,
) -> WheelCounts:
    """Combine the two motor-side pulse counters into one count sample.

    ``mapping`` is required: the left/right assignment of ``0x251``/``0x252``
    has not been verified, so it must come from a motion calibration.  The
    motor-side pulse scale is not the wheel scale; a calibration with
    ``source=SOURCE_ACTUATOR`` and the same mapping is still required.
    """
    if not isinstance(first, ActuatorHighSpeed) or not isinstance(
        second, ActuatorHighSpeed
    ):
        raise TypeError("pair_actuator_pulse needs two ActuatorHighSpeed samples")
    if not isinstance(mapping, PulseMapping):
        raise TypeError("pair_actuator_pulse needs an explicit PulseMapping")
    if first.iface != CHASSIS_BUS or second.iface != CHASSIS_BUS:
        raise ValueError("pulse pairing only accepts can0 chassis frames")
    if first.clock is None or second.clock is None:
        raise ValueError("pulse pairing needs explicit clock domains")
    if first.clock != second.clock:
        raise ValueError("pulse pairing refuses mixed clock domains")
    if first.timestamp is None or second.timestamp is None:
        raise ValueError("pulse pairing needs timestamps")
    by_id = {first.can_id: first, second.can_id: second}
    if set(by_id) != {mapping.left_can_id, mapping.right_can_id}:
        raise ValueError(
            "pulse frames %s do not match mapping %s"
            % (sorted(by_id), mapping)
        )
    timestamp = max(first.timestamp, second.timestamp)
    return WheelCounts(
        timestamp=timestamp,
        left=by_id[mapping.left_can_id].pulse_count,
        right=by_id[mapping.right_can_id].pulse_count,
        source=SOURCE_ACTUATOR,
        can_id=0,
        iface=CHASSIS_BUS,
        clock=first.clock,
        pulse_mapping=mapping,
    )


# ---------------------------------------------------------------------------
# Calibration and wheel motion
# ---------------------------------------------------------------------------


def _positive_finite(value) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value > 0.0
    )


def _finite_number(value) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


@dataclass(frozen=True)
class WheelCalibration:
    """Explicit scale from the selected count source to metres and radians.

    ``counts_per_meter`` is for the same source as ``source``.  A wheel-frame
    calibration must not be applied to motor pulses (and vice versa), so the
    source is part of the calibration identity.

    ``left_sign`` / ``right_sign`` are part of the calibration because a
    mirrored motor can count down while the wheel moves forward.  Resting
    cumulative values are not a direction calibration: determine the signs
    only by a forward-motion test where both wheel deltas have the same sign.

    ``pulse_mapping`` is required for ``SOURCE_ACTUATOR`` and must be the same
    mapping used by :func:`pair_actuator_pulse`.
    """

    counts_per_meter: float
    track_width_m: float
    source: str = SOURCE_WHEEL
    left_sign: int = 1
    right_sign: int = 1
    pulse_mapping: Optional[PulseMapping] = None

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if not _positive_finite(self.counts_per_meter):
            raise ValueError("counts_per_meter must be a positive finite number")
        if not _positive_finite(self.track_width_m):
            raise ValueError("track_width_m must be a positive finite number")
        if not isinstance(self.source, str) or not self.source:
            raise ValueError("source must be a non-empty string")
        for name in ("left_sign", "right_sign"):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value not in (-1, 1)
            ):
                raise ValueError("%s must be +1 or -1" % name)
        if self.source == SOURCE_ACTUATOR:
            if not isinstance(self.pulse_mapping, PulseMapping):
                raise ValueError(
                    "actuator_pulse calibration needs an explicit PulseMapping"
                )
        elif self.pulse_mapping is not None:
            raise ValueError("pulse_mapping is only valid for actuator_pulse")


@dataclass(frozen=True)
class WheelMotion:
    valid: bool
    code: str
    reason: str = ""
    distance_m: Optional[float] = None
    yaw_rad: Optional[float] = None
    left_m: Optional[float] = None
    right_m: Optional[float] = None
    dt_s: Optional[float] = None


def _invalid_motion(code: str, reason: str) -> WheelMotion:
    return WheelMotion(valid=False, code=code, reason=reason)


def wheel_motion(
    previous: WheelCounts,
    current: WheelCounts,
    calibration: Optional[WheelCalibration],
    max_dt_s: Optional[float] = None,
) -> WheelMotion:
    """Difference two count samples and convert to metres/radians.

    Without a matching calibration this returns ``valid=False``,
    ``code="UNCALIBRATED"`` and ``distance_m=None``.  It never invents a
    default counts-per-metre or wheel base.
    """
    if calibration is None:
        return _invalid_motion(
            "UNCALIBRATED",
            "no counts_per_meter / track_width calibration supplied",
        )
    if current.source != calibration.source or previous.source != current.source:
        return _invalid_motion(
            "UNCALIBRATED",
            "calibration source %r does not match sample source %r"
            % (calibration.source, current.source),
        )
    if current.clock is None or previous.clock is None:
        return _invalid_motion("CLOCK_UNKNOWN", "count samples need a clock domain")
    if current.clock != previous.clock:
        return _invalid_motion(
            "CLOCK_MISMATCH",
            "clock %r != %r" % (current.clock, previous.clock),
        )
    if (
        current.source == SOURCE_ACTUATOR
        and (
            previous.pulse_mapping != calibration.pulse_mapping
            or current.pulse_mapping != calibration.pulse_mapping
        )
    ):
        return _invalid_motion(
            "UNCALIBRATED",
            "pulse mapping does not match the calibration",
        )
    if previous.timestamp is None or current.timestamp is None:
        return _invalid_motion("STALE", "count samples need timestamps")
    dt = current.timestamp - previous.timestamp
    if dt <= 0.0:
        return _invalid_motion("STALE", "non-positive sample interval %.6f s" % dt)
    if max_dt_s is not None and dt > max_dt_s:
        return _invalid_motion(
            "STALE",
            "sample interval %.3f s > %.3f s" % (dt, max_dt_s),
        )

    left_m = (
        int32_delta(current.left, previous.left)
        * calibration.left_sign
        / calibration.counts_per_meter
    )
    right_m = (
        int32_delta(current.right, previous.right)
        * calibration.right_sign
        / calibration.counts_per_meter
    )
    distance_m = 0.5 * (left_m + right_m)
    yaw_rad = (right_m - left_m) / calibration.track_width_m
    return WheelMotion(
        valid=True,
        code="ok",
        distance_m=distance_m,
        yaw_rad=yaw_rad,
        left_m=left_m,
        right_m=right_m,
        dt_s=dt,
    )


# ---------------------------------------------------------------------------
# Chassis feedback quality gate
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GateDecision:
    allowed: bool
    code: str
    reason: str = ""

    @classmethod
    def ok(cls) -> "GateDecision":
        return cls(True, "ok", "")

    @classmethod
    def deny(cls, code: str, reason: str) -> "GateDecision":
        return cls(False, code, reason)


@dataclass
class FeedbackGateConfig:
    max_age_s: float = 0.25
    max_dt_s: float = 0.5
    max_count_jump: int = 500
    max_linear_mps: float = 0.20
    max_angular_rps: float = 0.50
    zero_epsilon_counts: int = 0
    clock: str = CLOCK_MONOTONIC

    def validate(self) -> None:
        if not _positive_finite(self.max_age_s):
            raise ValueError("max_age_s must be positive")
        if not _positive_finite(self.max_dt_s):
            raise ValueError("max_dt_s must be positive")
        if not isinstance(self.max_count_jump, int) or self.max_count_jump <= 0:
            raise ValueError("max_count_jump must be a positive integer")
        if not _positive_finite(self.max_linear_mps):
            raise ValueError("max_linear_mps must be positive")
        if not _positive_finite(self.max_angular_rps):
            raise ValueError("max_angular_rps must be positive")
        if not isinstance(self.zero_epsilon_counts, int) or self.zero_epsilon_counts < 0:
            raise ValueError("zero_epsilon_counts must be a non-negative integer")
        if not isinstance(self.clock, str) or not self.clock:
            raise ValueError("clock must be a non-empty string")


class ChassisFeedbackGate:
    """Reject feedback that must not enter a closed loop."""

    def __init__(
        self,
        config: Optional[FeedbackGateConfig] = None,
        calibration: Optional[WheelCalibration] = None,
    ):
        self.config = config or FeedbackGateConfig()
        self.config.validate()
        self.calibration = calibration

    def set_calibration(self, calibration: Optional[WheelCalibration]) -> None:
        self.calibration = calibration

    def evaluate(
        self,
        sample: Optional[WheelCounts],
        previous: Optional[WheelCounts],
        now: float,
        commanded_linear: float,
        commanded_angular: float,
    ) -> GateDecision:
        cfg = self.config
        if sample is None:
            return GateDecision.deny("NO_FEEDBACK", "no wheel feedback sample")
        if sample.timestamp is None:
            return GateDecision.deny("STALE", "sample has no timestamp")
        if not _finite_number(now) or not _finite_number(sample.timestamp):
            return GateDecision.deny("STALE", "non-finite timestamp")
        if sample.clock is None:
            return GateDecision.deny("CLOCK_UNKNOWN", "sample has no clock domain")
        if sample.clock != cfg.clock:
            return GateDecision.deny(
                "CLOCK_MISMATCH",
                "sample clock %r != %r" % (sample.clock, cfg.clock),
            )
        if not (
            _finite_number(sample.left) and _finite_number(sample.right)
        ):
            return GateDecision.deny("STALE", "non-finite wheel counts")
        age = now - sample.timestamp
        if age < -0.05:
            return GateDecision.deny("FUTURE", "feedback from the future: %.3f s" % age)
        if age > cfg.max_age_s:
            return GateDecision.deny(
                "STALE", "age %.3f s > %.3f s" % (age, cfg.max_age_s)
            )
        if self.calibration is None:
            return GateDecision.deny(
                "UNCALIBRATED",
                "no counts_per_meter / track_width calibration supplied",
            )
        if sample.source != self.calibration.source:
            return GateDecision.deny(
                "UNCALIBRATED",
                "sample source %r != calibration source %r"
                % (sample.source, self.calibration.source),
            )
        if previous is None:
            return GateDecision.ok()
        if previous.source != sample.source:
            return GateDecision.deny("STALE", "feedback source changed")
        if previous.clock is None:
            return GateDecision.deny("CLOCK_UNKNOWN", "previous sample has no clock")
        if previous.clock != sample.clock:
            return GateDecision.deny(
                "CLOCK_MISMATCH",
                "previous clock %r != %r" % (previous.clock, sample.clock),
            )
        if not (
            _finite_number(previous.left) and _finite_number(previous.right)
        ):
            return GateDecision.deny("STALE", "non-finite previous wheel counts")
        if previous.timestamp is None:
            return GateDecision.ok()
        dt = sample.timestamp - previous.timestamp
        if dt <= 0.0:
            return GateDecision.deny("STALE", "non-positive sample interval")
        if dt > cfg.max_dt_s:
            return GateDecision.deny(
                "STALE",
                "sample interval %.3f s > %.3f s" % (dt, cfg.max_dt_s),
            )
        d_left = int32_delta(sample.left, previous.left)
        d_right = int32_delta(sample.right, previous.right)
        if abs(d_left) > cfg.max_count_jump or abs(d_right) > cfg.max_count_jump:
            return GateDecision.deny(
                "JUMP",
                "count jump left=%d right=%d cap=%d"
                % (d_left, d_right, cfg.max_count_jump),
            )
        eps = cfg.zero_epsilon_counts
        zero = abs(d_left) <= eps and abs(d_right) <= eps
        if zero and (
            abs(commanded_linear) > 1e-9 or abs(commanded_angular) > 1e-9
        ):
            return GateDecision.deny(
                "ZERO_BUT_COMMANDED",
                "counts are constant while a nonzero motion is commanded",
            )
        motion = wheel_motion(previous, sample, self.calibration, cfg.max_dt_s)
        if not motion.valid:
            return GateDecision.deny(motion.code, motion.reason)
        linear = motion.distance_m / dt
        angular = motion.yaw_rad / dt
        if abs(linear) > cfg.max_linear_mps + 1e-9:
            return GateDecision.deny(
                "OUT_OF_RANGE", "linear %.3f m/s above cap" % linear
            )
        if abs(angular) > cfg.max_angular_rps + 1e-9:
            return GateDecision.deny(
                "OUT_OF_RANGE", "angular %.3f rad/s above cap" % angular
            )
        return GateDecision.ok()


# ---------------------------------------------------------------------------
# Chassis velocity loop
# ---------------------------------------------------------------------------


class ChassisStatus(str, Enum):
    MOVING = "MOVING"
    HOLD = "HOLD"
    WARMUP = "WARMUP"
    NO_FEEDBACK = "NO_FEEDBACK"
    FUTURE = "FUTURE"
    STALE = "STALE"
    CLOCK_UNKNOWN = "CLOCK_UNKNOWN"
    CLOCK_MISMATCH = "CLOCK_MISMATCH"
    UNCALIBRATED = "UNCALIBRATED"
    ZERO_BUT_COMMANDED = "ZERO_BUT_COMMANDED"
    JUMP = "JUMP"
    OUT_OF_RANGE = "OUT_OF_RANGE"
    ESTOP = "ESTOP"
    SAFETY = "SAFETY"


@dataclass(frozen=True)
class ChassisCommand:
    linear_mps: float
    angular_rps: float
    ttl_s: float
    status: ChassisStatus
    source: str = "none"
    reason: str = ""
    measured_linear_mps: Optional[float] = None
    measured_angular_rps: Optional[float] = None


@dataclass
class ChassisControlConfig:
    kp_linear: float = 0.6
    kp_angular: float = 1.0
    max_linear_mps: float = 0.08
    max_angular_rps: float = 0.15
    max_linear_step_mps: float = 0.02
    max_angular_step_rps: float = 0.05
    velocity_tolerance_mps: float = 0.005
    velocity_tolerance_rps: float = 0.01
    command_ttl_s: float = 0.2
    clock: str = CLOCK_MONOTONIC
    gate: FeedbackGateConfig = field(default_factory=FeedbackGateConfig)

    def validate(self) -> None:
        for name in (
            "kp_linear",
            "kp_angular",
            "max_linear_mps",
            "max_angular_rps",
            "max_linear_step_mps",
            "max_angular_step_rps",
            "velocity_tolerance_mps",
            "velocity_tolerance_rps",
            "command_ttl_s",
        ):
            if not _positive_finite(getattr(self, name)):
                raise ValueError("%s must be a positive finite number" % name)
        if self.command_ttl_s > 0.5 + 1e-12:
            raise ValueError("command_ttl_s must be <= 0.5 s")
        if not isinstance(self.clock, str) or not self.clock:
            raise ValueError("clock must be a non-empty string")
        if self.gate.clock != self.clock:
            raise ValueError("gate clock must match controller clock")


_GATE_STATUS = {
    "NO_FEEDBACK": ChassisStatus.NO_FEEDBACK,
    "FUTURE": ChassisStatus.FUTURE,
    "STALE": ChassisStatus.STALE,
    "CLOCK_UNKNOWN": ChassisStatus.CLOCK_UNKNOWN,
    "CLOCK_MISMATCH": ChassisStatus.CLOCK_MISMATCH,
    "UNCALIBRATED": ChassisStatus.UNCALIBRATED,
    "ZERO_BUT_COMMANDED": ChassisStatus.ZERO_BUT_COMMANDED,
    "JUMP": ChassisStatus.JUMP,
    "OUT_OF_RANGE": ChassisStatus.OUT_OF_RANGE,
}


class ChassisVelocityController:
    """Velocity loop that only runs while the feedback gate allows it."""

    def __init__(
        self,
        config: Optional[ChassisControlConfig] = None,
        calibration: Optional[WheelCalibration] = None,
    ):
        self.config = config or ChassisControlConfig()
        self.config.validate()
        self.calibration = calibration
        self.gate = ChassisFeedbackGate(self.config.gate, calibration)
        self.last_sample: Optional[WheelCounts] = None
        self.last_command = (0.0, 0.0)
        self.estop = False

    def set_calibration(self, calibration: Optional[WheelCalibration]) -> None:
        self.calibration = calibration
        self.gate.set_calibration(calibration)
        self.last_sample = None
        self.last_command = (0.0, 0.0)

    def emergency_stop(self) -> None:
        self.estop = True

    def reset(self) -> None:
        self.last_sample = None
        self.last_command = (0.0, 0.0)
        self.estop = False

    def step(
        self,
        now: float,
        target_linear_mps: float,
        target_angular_rps: float,
        sample: Optional[WheelCounts],
    ) -> ChassisCommand:
        if self.estop:
            return self._zero(ChassisStatus.ESTOP, "emergency stop")
        if not (
            _finite_number(target_linear_mps)
            and _finite_number(target_angular_rps)
        ):
            return self._zero(ChassisStatus.SAFETY, "non-finite velocity target")
        previous = self.last_sample
        decision = self.gate.evaluate(
            sample,
            previous,
            now,
            target_linear_mps,
            target_angular_rps,
        )
        if (
            sample is not None
            and sample.timestamp is not None
            and _finite_number(sample.timestamp)
            and _finite_number(sample.left)
            and _finite_number(sample.right)
            and decision.code not in (
                "FUTURE", "JUMP", "CLOCK_UNKNOWN", "CLOCK_MISMATCH"
            )
            and (previous is None or sample.timestamp >= previous.timestamp)
        ):
            self.last_sample = sample
        source = sample.source if sample is not None else "none"
        if not decision.allowed:
            status = _GATE_STATUS.get(decision.code, ChassisStatus.NO_FEEDBACK)
            return self._zero(status, "%s: %s" % (decision.code, decision.reason), source)
        if previous is None:
            return self._zero(
                ChassisStatus.WARMUP,
                "first feedback sample establishes a baseline",
                source,
            )
        motion = wheel_motion(previous, sample, self.calibration,
                              self.config.gate.max_dt_s)
        if not motion.valid:
            status = _GATE_STATUS.get(motion.code, ChassisStatus.NO_FEEDBACK)
            return self._zero(status, motion.reason, source)
        dt = motion.dt_s
        measured_linear = motion.distance_m / dt
        measured_angular = motion.yaw_rad / dt
        cfg = self.config
        error_linear = target_linear_mps - measured_linear
        error_angular = target_angular_rps - measured_angular
        if (
            abs(error_linear) <= cfg.velocity_tolerance_mps
            and abs(error_angular) <= cfg.velocity_tolerance_rps
        ):
            linear = _clamp(target_linear_mps, -cfg.max_linear_mps, cfg.max_linear_mps)
            angular = _clamp(target_angular_rps, -cfg.max_angular_rps, cfg.max_angular_rps)
            self.last_command = (linear, angular)
            return ChassisCommand(
                linear_mps=linear,
                angular_rps=angular,
                ttl_s=cfg.command_ttl_s,
                status=ChassisStatus.HOLD,
                source=source,
                reason="within velocity tolerance",
                measured_linear_mps=measured_linear,
                measured_angular_rps=measured_angular,
            )
        linear = _clamp(
            target_linear_mps + cfg.kp_linear * error_linear,
            -cfg.max_linear_mps,
            cfg.max_linear_mps,
        )
        angular = _clamp(
            target_angular_rps + cfg.kp_angular * error_angular,
            -cfg.max_angular_rps,
            cfg.max_angular_rps,
        )
        linear = _step_limit(linear, self.last_command[0], cfg.max_linear_step_mps)
        angular = _step_limit(angular, self.last_command[1], cfg.max_angular_step_rps)
        if (
            abs(linear) > cfg.max_linear_mps + 1e-9
            or abs(angular) > cfg.max_angular_rps + 1e-9
            or cfg.command_ttl_s <= 0.0
        ):
            return self._zero(ChassisStatus.SAFETY, "command outside safety limits", source)
        self.last_command = (linear, angular)
        return ChassisCommand(
            linear_mps=linear,
            angular_rps=angular,
            ttl_s=cfg.command_ttl_s,
            status=ChassisStatus.MOVING,
            source=source,
            reason="closed-loop",
            measured_linear_mps=measured_linear,
            measured_angular_rps=measured_angular,
        )

    def _zero(
        self,
        status: ChassisStatus,
        reason: str,
        source: str = "none",
    ) -> ChassisCommand:
        self.last_command = (0.0, 0.0)
        return ChassisCommand(
            linear_mps=0.0,
            angular_rps=0.0,
            ttl_s=0.0,
            status=status,
            source=source,
            reason=reason,
        )


def _clamp(value: float, low: float, high: float) -> float:
    return low if value < low else high if value > high else value


def _step_limit(value: float, previous: float, max_step: float) -> float:
    return _clamp(value, previous - max_step, previous + max_step)


# ---------------------------------------------------------------------------
# Arm small-step loop
# ---------------------------------------------------------------------------


class ArmStatus(str, Enum):
    MOVING = "MOVING"
    CONVERGED = "CONVERGED"
    STALLED = "STALLED"
    TIMEOUT = "TIMEOUT"
    MAX_ITERATIONS = "MAX_ITERATIONS"
    OVERSHOOT = "OVERSHOOT"
    FAULT = "FAULT"
    MODE = "MODE"
    DISABLED = "DISABLED"
    FEEDBACK_LOST = "FEEDBACK_LOST"
    CLOCK_UNKNOWN = "CLOCK_UNKNOWN"
    CLOCK_MISMATCH = "CLOCK_MISMATCH"
    UNREACHABLE = "UNREACHABLE"
    LAG = "LAG"
    SAFETY = "SAFETY"
    ESTOP = "ESTOP"


DEFAULT_JOINT_LIMITS = (
    (-150.0, 150.0),
    (0.0, 180.0),
    (-170.0, 0.0),
    (-100.0, 100.0),
    (-70.0, 70.0),
    (-120.0, 120.0),
)


@dataclass(frozen=True)
class ArmFeedback:
    timestamp: float
    joints_deg: Tuple[float, ...]
    mode: str = "MOVE_J"
    fault: Optional[str] = None
    enabled: Optional[bool] = None
    lag_deg: Optional[float] = None
    ctrl_mode: int = 1
    motion_status: int = 0
    clock: Optional[str] = None
    iface: Optional[str] = None

    def age(self, now: float) -> float:
        return now - self.timestamp


@dataclass(frozen=True)
class ArmCommand:
    target_joints_deg: Tuple[float, ...]
    status: ArmStatus
    reason: str = ""
    step_deg: float = 0.0
    iteration: int = 0
    error_deg: float = 0.0


@dataclass
class ArmControlConfig:
    max_step_deg: float = 0.5
    tolerance_deg: float = 0.15
    settle_steps: int = 2
    timeout_s: float = 10.0
    max_iterations: int = 200
    gain: float = 0.5
    max_overshoots: int = 2
    stall_eps_deg: float = 0.02
    stall_iterations: int = 3
    max_feedback_age_s: float = 0.25
    max_lag_deg: float = 1.5
    limit_margin_deg: float = 5.0
    clock: str = CLOCK_MONOTONIC
    joint_limits_deg: Tuple[Tuple[float, float], ...] = DEFAULT_JOINT_LIMITS

    def validate(self) -> None:
        if not _positive_finite(self.max_step_deg) or self.max_step_deg > 0.5:
            raise ValueError("max_step_deg must be in (0, 0.5]")
        if (
            not _positive_finite(self.tolerance_deg)
            or self.tolerance_deg > self.max_step_deg
        ):
            raise ValueError("tolerance_deg must be in (0, max_step_deg]")
        if not isinstance(self.settle_steps, int) or self.settle_steps < 1:
            raise ValueError("settle_steps must be >= 1")
        if not _positive_finite(self.timeout_s):
            raise ValueError("timeout_s must be positive")
        if not isinstance(self.max_iterations, int) or self.max_iterations < 1:
            raise ValueError("max_iterations must be >= 1")
        if not _positive_finite(self.gain) or self.gain > 1.0:
            raise ValueError("gain must be in (0, 1]")
        if not isinstance(self.max_overshoots, int) or self.max_overshoots < 0:
            raise ValueError("max_overshoots must be >= 0")
        if not _positive_finite(self.stall_eps_deg):
            raise ValueError("stall_eps_deg must be positive")
        if not isinstance(self.stall_iterations, int) or self.stall_iterations < 1:
            raise ValueError("stall_iterations must be >= 1")
        if not _positive_finite(self.max_feedback_age_s):
            raise ValueError("max_feedback_age_s must be positive")
        if not _positive_finite(self.max_lag_deg):
            raise ValueError("max_lag_deg must be positive")
        if (
            not isinstance(self.limit_margin_deg, (int, float))
            or isinstance(self.limit_margin_deg, bool)
            or not math.isfinite(self.limit_margin_deg)
            or self.limit_margin_deg < 0.0
        ):
            raise ValueError("limit_margin_deg must be finite and >= 0")
        if not isinstance(self.clock, str) or not self.clock:
            raise ValueError("clock must be a non-empty string")
        if len(self.joint_limits_deg) != 6:
            raise ValueError("joint_limits_deg must have 6 pairs")
        for pair in self.joint_limits_deg:
            if len(pair) != 2:
                raise ValueError("each joint limit must be (low, high)")
            low, high = pair
            if not (
                isinstance(low, (int, float))
                and isinstance(high, (int, float))
                and not isinstance(low, bool)
                and not isinstance(high, bool)
                and math.isfinite(low)
                and math.isfinite(high)
                and low < high
            ):
                raise ValueError("each joint limit must be finite and low < high")


class JointSetpointController:
    """Small-step joint loop for one or more Piper joints.

    Every command is an absolute joint vector whose per-joint change from the
    measured pose is at most ``max_step_deg`` (default 0.5 deg).  The
    controller is an outer loop over the arm's own position servo; it never
    sends raw motor effort.
    """

    def __init__(
        self,
        target_joints_deg: Sequence[float],
        config: Optional[ArmControlConfig] = None,
    ):
        self.target = tuple(float(v) for v in target_joints_deg)
        if not 1 <= len(self.target) <= 6:
            raise ValueError("target must have 1..6 joints")
        if any(not _finite_number(v) for v in self.target):
            raise ValueError("target joints must be finite")
        self.config = config or ArmControlConfig()
        self.config.validate()
        self.gain = self.config.gain
        self.start_time: Optional[float] = None
        self.iteration = 0
        self.last_measured: Optional[Tuple[float, ...]] = None
        self.last_error: Optional[Tuple[float, ...]] = None
        self.last_command: Optional[Tuple[float, ...]] = None
        self.overshoot_count = 0
        self.settle_count = 0
        self.stall_count = 0
        self.estop = False
        self.status = ArmStatus.MOVING

    def emergency_stop(self) -> None:
        self.estop = True
        self.status = ArmStatus.ESTOP

    def reset(self) -> None:
        self.gain = self.config.gain
        self.start_time = None
        self.iteration = 0
        self.last_measured = None
        self.last_error = None
        self.last_command = None
        self.overshoot_count = 0
        self.settle_count = 0
        self.stall_count = 0
        self.estop = False
        self.status = ArmStatus.MOVING

    def step(self, now: float, feedback: Optional[ArmFeedback]) -> ArmCommand:
        if self.estop:
            return self._result(
                self._hold_joints(feedback),
                ArmStatus.ESTOP,
                "emergency stop",
                iteration=self.iteration,
            )
        if feedback is None:
            self.status = ArmStatus.FEEDBACK_LOST
            return self._result(
                self._hold_joints(None),
                ArmStatus.FEEDBACK_LOST,
                "no joint feedback",
                iteration=self.iteration,
            )
        measured = tuple(float(v) for v in feedback.joints_deg)
        if len(measured) != len(self.target):
            self.status = ArmStatus.FEEDBACK_LOST
            return self._result(
                self._hold_joints(None),
                ArmStatus.FEEDBACK_LOST,
                "joint count mismatch: target %d, feedback %d"
                % (len(self.target), len(measured)),
                iteration=self.iteration,
            )
        if (
            not _finite_number(now)
            or not _finite_number(feedback.timestamp)
            or any(not _finite_number(v) for v in measured)
        ):
            self.status = ArmStatus.FEEDBACK_LOST
            return self._result(
                self._hold_joints(None),
                ArmStatus.FEEDBACK_LOST,
                "non-finite joint feedback or timestamp",
                iteration=self.iteration,
            )
        age = feedback.age(now)
        if age < -0.05:
            self.status = ArmStatus.FEEDBACK_LOST
            return self._result(
                measured,
                ArmStatus.FEEDBACK_LOST,
                "joint feedback from the future: age %.3f s" % age,
                iteration=self.iteration,
            )
        if age > self.config.max_feedback_age_s:
            self.status = ArmStatus.FEEDBACK_LOST
            return self._result(
                measured,
                ArmStatus.FEEDBACK_LOST,
                "stale joint feedback: age %.3f s" % age,
                iteration=self.iteration,
            )
        if feedback.fault:
            self.status = ArmStatus.FAULT
            return self._result(
                measured, ArmStatus.FAULT, str(feedback.fault), iteration=self.iteration
            )
        if feedback.clock is None:
            self.status = ArmStatus.CLOCK_UNKNOWN
            return self._result(
                measured,
                ArmStatus.CLOCK_UNKNOWN,
                "feedback clock is unknown",
                iteration=self.iteration,
            )
        if feedback.clock != self.config.clock:
            self.status = ArmStatus.CLOCK_MISMATCH
            return self._result(
                measured,
                ArmStatus.CLOCK_MISMATCH,
                "feedback clock %r != %r" % (feedback.clock, self.config.clock),
                iteration=self.iteration,
            )
        if feedback.enabled is None:
            self.status = ArmStatus.DISABLED
            return self._result(
                measured,
                ArmStatus.DISABLED,
                "arm enable state unknown",
                iteration=self.iteration,
            )
        if not feedback.enabled:
            self.status = ArmStatus.DISABLED
            return self._result(
                measured, ArmStatus.DISABLED, "arm disabled", iteration=self.iteration
            )
        if feedback.ctrl_mode != 1:
            self.status = ArmStatus.MODE
            return self._result(
                measured,
                ArmStatus.MODE,
                "ctrl_mode=%d (expected CAN=1)" % feedback.ctrl_mode,
                iteration=self.iteration,
            )
        if feedback.mode not in ("MOVE_J", "MOVE_P"):
            self.status = ArmStatus.MODE
            return self._result(
                measured,
                ArmStatus.MODE,
                "mode %r not MOVE_J/MOVE_P" % (feedback.mode,),
                iteration=self.iteration,
            )
        if feedback.lag_deg is None:
            self.status = ArmStatus.LAG
            return self._result(
                measured,
                ArmStatus.LAG,
                "lag is unknown",
                iteration=self.iteration,
            )
        if (
            not _finite_number(feedback.lag_deg)
            or feedback.lag_deg < 0.0
        ):
            self.status = ArmStatus.LAG
            return self._result(
                measured,
                ArmStatus.LAG,
                "lag is not a finite non-negative value",
                iteration=self.iteration,
            )
        if feedback.lag_deg > self.config.max_lag_deg:
            self.status = ArmStatus.LAG
            return self._result(
                measured,
                ArmStatus.LAG,
                "lag %.3f > %.3f deg"
                % (feedback.lag_deg, self.config.max_lag_deg),
                iteration=self.iteration,
            )

        if self.start_time is None:
            self.start_time = now
        self.iteration += 1
        for index, target in enumerate(self.target):
            low, high = self.config.joint_limits_deg[index]
            if not low <= target <= high:
                self.status = ArmStatus.UNREACHABLE
                return self._result(
                    measured,
                    ArmStatus.UNREACHABLE,
                    "target joint %d=%s outside [%s, %s]"
                    % (index + 1, target, low, high),
                    iteration=self.iteration,
                )
        for index, value in enumerate(measured):
            low, high = self.config.joint_limits_deg[index]
            margin = self.config.limit_margin_deg
            if value < low - margin or value > high + margin:
                self.status = ArmStatus.SAFETY
                return self._result(
                    measured,
                    ArmStatus.SAFETY,
                    "joint %d measured %.3f outside margin [%.3f, %.3f]"
                    % (index + 1, value, low - margin, high + margin),
                    iteration=self.iteration,
                )

        error = tuple(t - m for t, m in zip(self.target, measured))
        max_error = max(abs(v) for v in error)
        if max_error > self.config.tolerance_deg:
            self.settle_count = 0

        if max_error <= self.config.tolerance_deg:
            self.settle_count += 1
            if self.settle_count >= self.config.settle_steps:
                self.status = ArmStatus.CONVERGED
                self.last_measured = measured
                self.last_error = error
                return self._result(
                    self.target,
                    ArmStatus.CONVERGED,
                    "within tolerance",
                    step_deg=max_error,
                    iteration=self.iteration,
                    error_deg=max_error,
                )
            self.last_measured = measured
            self.last_error = error
            return self._result(
                self.target,
                ArmStatus.MOVING,
                "settling",
                step_deg=max_error,
                iteration=self.iteration,
                error_deg=max_error,
            )

        if now - self.start_time > self.config.timeout_s:
            self.status = ArmStatus.TIMEOUT
            return self._result(
                measured, ArmStatus.TIMEOUT, "timeout", iteration=self.iteration
            )
        if self.iteration > self.config.max_iterations:
            self.status = ArmStatus.MAX_ITERATIONS
            return self._result(
                measured,
                ArmStatus.MAX_ITERATIONS,
                "max iterations",
                iteration=self.iteration,
            )

        if self.last_error is not None:
            reversed_joint = any(
                abs(prev) > self.config.tolerance_deg
                and abs(curr) > self.config.tolerance_deg
                and prev * curr < 0
                for prev, curr in zip(self.last_error, error)
            )
            if reversed_joint:
                self.overshoot_count += 1
                self.gain = max(self.gain * 0.5, 0.05)
                if self.overshoot_count > self.config.max_overshoots:
                    self.status = ArmStatus.OVERSHOOT
                    return self._result(
                        measured,
                        ArmStatus.OVERSHOOT,
                        "repeated overshoot (%d)" % self.overshoot_count,
                        iteration=self.iteration,
                    )

        if self.last_measured is not None:
            movement = max(
                abs(m - last) for m, last in zip(measured, self.last_measured)
            )
            if movement < self.config.stall_eps_deg:
                self.stall_count += 1
                if self.stall_count >= self.config.stall_iterations:
                    self.status = ArmStatus.STALLED
                    return self._result(
                        measured,
                        ArmStatus.STALLED,
                        "no movement for %d iterations" % self.stall_count,
                        iteration=self.iteration,
                    )
            else:
                self.stall_count = 0

        step = []
        for value in error:
            commanded = _clamp(
                self.gain * value,
                -self.config.max_step_deg,
                self.config.max_step_deg,
            )
            if abs(commanded) > abs(value):
                commanded = value
            step.append(commanded)
        target_command = tuple(m + s for m, s in zip(measured, step))
        step_abs = max(abs(v) for v in step)

        for index, (m, t) in enumerate(zip(measured, target_command)):
            low, high = self.config.joint_limits_deg[index]
            if abs(t - m) > self.config.max_step_deg + 1e-12:
                self.status = ArmStatus.SAFETY
                return self._result(
                    measured,
                    ArmStatus.SAFETY,
                    "joint %d step exceeds cap" % (index + 1),
                    iteration=self.iteration,
                )
            if m < low:
                allowed_low, allowed_high = m, high
            elif m > high:
                allowed_low, allowed_high = low, m
            else:
                allowed_low, allowed_high = low, high
            if t < allowed_low - 1e-12 or t > allowed_high + 1e-12:
                self.status = ArmStatus.SAFETY
                return self._result(
                    measured,
                    ArmStatus.SAFETY,
                    "joint %d target moves further outside limits" % (index + 1),
                    iteration=self.iteration,
                )

        self.last_measured = measured
        self.last_error = error
        self.last_command = target_command
        self.status = ArmStatus.MOVING
        return self._result(
            target_command,
            ArmStatus.MOVING,
            "step",
            step_deg=step_abs,
            iteration=self.iteration,
            error_deg=max_error,
        )

    def _hold_joints(self, feedback: Optional[ArmFeedback]) -> Tuple[float, ...]:
        if feedback is not None:
            return tuple(float(v) for v in feedback.joints_deg)
        if self.last_measured is not None:
            return self.last_measured
        return ()

    def _result(
        self,
        target: Sequence[float],
        status: ArmStatus,
        reason: str,
        step_deg: float = 0.0,
        iteration: int = 0,
        error_deg: float = 0.0,
    ) -> ArmCommand:
        return ArmCommand(
            target_joints_deg=tuple(float(v) for v in target),
            status=status,
            reason=reason,
            step_deg=float(step_deg),
            iteration=int(iteration),
            error_deg=float(error_deg),
        )


class ArmFeedbackAssembler:
    """Merge ``0x2A5/0x2A6/0x2A7`` and ``0x2A1`` into one feedback sample.

    A snapshot is produced only when all six joints and a status frame are
    present.  Assuming a mode when the status is missing would make the mode
    gate meaningless, so the assembler refuses instead.
    """

    def __init__(self) -> None:
        self._joints = [None] * 6
        self._joint_timestamps = [None] * 6
        self._joint_ifaces = [None] * 6
        self._joint_clocks = [None] * 6
        self._status: Optional[ArmStatusFeedback] = None
        self._status_timestamp: Optional[float] = None

    def feed(self, parsed) -> None:
        if isinstance(parsed, ArmJointPair):
            for offset, angle in enumerate(parsed.joints_deg):
                index = parsed.first_joint - 1 + offset
                if not 0 <= index < 6:
                    raise ValueError("joint index %d outside 1..6" % (index + 1))
                self._joints[index] = float(angle)
                self._joint_timestamps[index] = parsed.timestamp
                self._joint_ifaces[index] = parsed.iface
                self._joint_clocks[index] = parsed.clock
            return
        if isinstance(parsed, ArmStatusFeedback):
            self._status = parsed
            self._status_timestamp = parsed.timestamp
            return
        raise TypeError("assembler accepts ArmJointPair or ArmStatusFeedback")

    def snapshot(
        self,
        enabled: Optional[bool] = None,
        lag_deg: Optional[float] = None,
        target_joints_deg: Optional[Sequence[float]] = None,
        expected_clock: Optional[str] = None,
    ) -> Optional[ArmFeedback]:
        """Build a feedback sample without inventing enable/lag values.

        ``enabled`` must come from the real motor-enable state; ``lag_deg``
        can be supplied directly or derived from ``target_joints_deg`` as the
        largest absolute target/measured gap.  Neither defaults to a safe
        looking ``True``/``0``: the controller rejects unknown values.
        """
        if self._status is None or any(v is None for v in self._joints):
            return None
        stamps = list(self._joint_timestamps) + [self._status_timestamp]
        ifaces = list(self._joint_ifaces) + [self._status.iface]
        clocks = list(self._joint_clocks) + [self._status.clock]
        if any(v is None for v in stamps + ifaces + clocks):
            return None
        if len(set(ifaces)) != 1 or len(set(clocks)) != 1:
            return None
        clock = clocks[0]
        if expected_clock is not None and expected_clock != clock:
            return None

        if enabled is not None and not isinstance(enabled, bool):
            raise ValueError("enabled must be True, False or None")
        if lag_deg is not None:
            if not _finite_number(lag_deg) or lag_deg < 0.0:
                raise ValueError("lag_deg must be finite and >= 0")
            lag = float(lag_deg)
        elif target_joints_deg is not None:
            target = tuple(float(v) for v in target_joints_deg)
            if len(target) != 6 or any(not _finite_number(v) for v in target):
                raise ValueError("target_joints_deg must be six finite values")
            lag = max(
                abs(t - m) for t, m in zip(target, self._joints)
            )
        else:
            lag = None

        # The oldest contributing frame defines the sample age, so a fresh
        # joint frame cannot mask a stale status frame (or vice versa).
        timestamp = min(stamps)
        return ArmFeedback(
            timestamp=timestamp,
            joints_deg=tuple(self._joints),
            mode=self._status.mode_name,
            fault=self._status.fault,
            enabled=enabled,
            lag_deg=lag,
            ctrl_mode=self._status.ctrl_mode,
            motion_status=self._status.motion_status,
            clock=clock,
            iface=ifaces[0],
        )


__all__ = [
    "CAN_ID_ODOMETRY",
    "CAN_ID_ACTUATOR1_HS",
    "CAN_ID_ACTUATOR2_HS",
    "CAN_ID_ARM_STATUS",
    "CAN_ID_ARM_JOINT_12",
    "CAN_ID_ARM_JOINT_34",
    "CAN_ID_ARM_JOINT_56",
    "FRAME_LEN",
    "CHASSIS_BUS",
    "ARM_BUSES",
    "CLOCK_MONOTONIC",
    "CLOCK_EPOCH",
    "SOURCE_WHEEL",
    "SOURCE_ACTUATOR",
    "FrameError",
    "UnsupportedFrame",
    "FrameBusError",
    "CanFrame",
    "parse_candump_line",
    "decode_frame",
    "int32_delta",
    "parse_wheel_counts",
    "parse_actuator_hs",
    "parse_arm_status",
    "parse_arm_joints",
    "WheelCounts",
    "ActuatorHighSpeed",
    "ArmJointPair",
    "ArmStatusFeedback",
    "PulseMapping",
    "pair_actuator_pulse",
    "WheelCalibration",
    "WheelMotion",
    "wheel_motion",
    "GateDecision",
    "FeedbackGateConfig",
    "ChassisFeedbackGate",
    "ChassisStatus",
    "ChassisCommand",
    "ChassisControlConfig",
    "ChassisVelocityController",
    "ArmStatus",
    "ArmFeedback",
    "ArmCommand",
    "ArmControlConfig",
    "JointSetpointController",
    "ArmFeedbackAssembler",
    "DEFAULT_JOINT_LIMITS",
]
