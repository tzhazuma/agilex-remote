#!/usr/bin/env python3
"""Rule-based calibration-board grasp for Tracer + dual Piper.

This module is the deterministic half of a board grasp: it validates every
number, splits a large joint goal into <=2 deg relative ``/api/arm`` nudges,
runs a fail-closed state machine, and only reports success after fresh visual
evidence.  Model and VLM output is advisory: it is logged and never consulted
by a safety or success decision.

The default mode is an offline dry run.  Real HTTP is reachable only through
``--execute`` *and* two more tokens::

    python3 tools/rule-grasp.py --demo
    python3 tools/rule-grasp.py --plan board.json \
        --execute --confirm EXECUTE --people-clear \
        --vision my_detector:probe

The server's ``/api/arm`` handler coerces a missing/odd ``arm`` field with
``int()`` and falls back to the left arm; a string ``"right"`` therefore used
to move the left arm.  This module refuses anything except the integers 0 and 1
before a request is built, and the transport logs the exact body it sent.

Absolute targets, reset/home, mode/MIT/MOVEC, teach/master-slave, trajectory
playback and E-STOP clearing are deliberately not implemented.  E-STOP itself
is never sent by this module; the operator's physical stop remains primary.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
import sys
import time
import urllib.request
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

# The server clamps every /api/arm delta to this value
# (AGILEX_MAX_JOINT_STEP).  A client-side step above it would be silently
# truncated, so plans never ask for more.
MAX_API_STEP_DEG = 2.0
GRIPPER_OPEN_DEG = 70.0
ARM_SIDES = {0: "left", 1: "right"}

# Documented Piper joint limits, degrees, same table as the server.
JOINT_LIMITS_DEG = (
    (-150.0, 150.0),
    (0.0, 180.0),
    (-170.0, 0.0),
    (-100.0, 100.0),
    (-70.0, 70.0),
    (-120.0, 120.0),
)


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------


class RuleGraspError(RuntimeError):
    """Base class for this module."""


class ValidationError(RuleGraspError, ValueError):
    """A number, plan or CLI argument is not acceptable."""


class SafetyAbort(RuleGraspError):
    """The state machine stopped; ``code`` is stable for logs and tests."""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__("%s: %s" % (code, detail) if detail else code)


def validate_arm(value: Any) -> int:
    """Return 0 or 1, or raise.  ``True`` is not an arm number."""
    if isinstance(value, bool) or not isinstance(value, int) or value not in (0, 1):
        raise ValidationError(
            "arm must be the integer 0 (left) or 1 (right), got %r" % (value,)
        )
    return value


def validate_joint(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 5:
        raise ValidationError("joint must be an integer 0..5, got %r" % (value,))
    return value


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError("%s must be a finite number, got %r" % (name, value))
    out = float(value)
    if not math.isfinite(out):
        raise ValidationError("%s must be finite, got %r" % (name, value))
    return out


def validate_delta(delta_deg: Any, max_step_deg: float = MAX_API_STEP_DEG) -> float:
    delta = _finite(delta_deg, "delta_deg")
    max_step = _finite(max_step_deg, "max_step_deg")
    if max_step <= 0.0 or max_step > MAX_API_STEP_DEG + 1e-9:
        raise ValidationError(
            "max_step_deg must be in (0, %.1f], got %r"
            % (MAX_API_STEP_DEG, max_step_deg)
        )
    if abs(delta) > max_step + 1e-9:
        raise ValidationError(
            "delta_deg %.6f exceeds the %.3f deg per-command cap"
            % (delta, max_step)
        )
    return delta


def _target_in_limits(joint: int, current: float, target: float) -> bool:
    """A target must be inside the box, or move an out-of-box pose inward.

    The real left J2 rests at about -2 deg although its documented range is
    0..180.  Refusing every move from that pose would make the arm unusable, so
    a target outside the box is allowed only when it is strictly closer to the
    box than the measured pose.
    """
    lo, hi = JOINT_LIMITS_DEG[joint]
    if lo <= target <= hi:
        return True
    if current < lo:
        return current <= target <= hi
    if current > hi:
        return lo <= target <= current
    return False


def split_delta(delta_deg: float, max_step_deg: float = MAX_API_STEP_DEG) -> List[float]:
    """Split one joint delta into deterministic pieces no larger than the cap."""
    delta = _finite(delta_deg, "delta_deg")
    max_step = _finite(max_step_deg, "max_step_deg")
    if max_step <= 0.0 or max_step > MAX_API_STEP_DEG + 1e-9:
        raise ValidationError("max_step_deg must be in (0, 2.0]")
    if abs(delta) <= 1e-9:
        return []
    sign = 1.0 if delta > 0.0 else -1.0
    remaining = abs(delta)
    out: List[float] = []
    while remaining > 1e-9:
        piece = min(remaining, max_step)
        out.append(round(sign * piece, 6))
        remaining -= piece
        if len(out) > 10000:  # pragma: no cover - defensive
            raise ValidationError("delta would need too many steps")
    residue = round(delta - sum(out), 6)
    if abs(residue) > 1e-9:
        out[-1] = round(out[-1] + residue, 6)
    return out


@dataclass(frozen=True)
class ArmCommand:
    """Exactly the body ``/api/arm`` accepts, with strict types."""

    arm: int
    joint: int
    delta_deg: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "arm", validate_arm(self.arm))
        object.__setattr__(self, "joint", validate_joint(self.joint))
        object.__setattr__(self, "delta_deg", validate_delta(self.delta_deg))

    def body(self) -> Dict[str, Any]:
        return {"arm": self.arm, "joint": self.joint,
                "delta_deg": float(self.delta_deg)}

    def as_dict(self) -> Dict[str, Any]:
        return self.body()


@dataclass(frozen=True)
class GripperCommand:
    arm: int
    delta_deg: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "arm", validate_arm(self.arm))
        object.__setattr__(self, "delta_deg", _finite(self.delta_deg, "delta_deg"))

    def body(self) -> Dict[str, Any]:
        return {"arm": self.arm, "delta_deg": float(self.delta_deg)}


@dataclass(frozen=True)
class PlanBudget:
    max_step_deg: float = MAX_API_STEP_DEG
    max_total_deg_per_joint: float = 45.0
    max_commands: int = 128
    max_duration_s: float = 180.0

    def __post_init__(self) -> None:
        step = _finite(self.max_step_deg, "max_step_deg")
        if not 0.0 < step <= MAX_API_STEP_DEG + 1e-9:
            raise ValidationError("max_step_deg must be in (0, 2.0]")
        total = _finite(self.max_total_deg_per_joint, "max_total_deg_per_joint")
        if total <= 0.0:
            raise ValidationError("max_total_deg_per_joint must be positive")
        if isinstance(self.max_commands, bool) or not isinstance(self.max_commands, int) \
                or self.max_commands <= 0:
            raise ValidationError("max_commands must be a positive integer")
        duration = _finite(self.max_duration_s, "max_duration_s")
        if duration <= 0.0:
            raise ValidationError("max_duration_s must be positive")


@dataclass(frozen=True)
class JointGoal:
    """One requested joint change, as an absolute target or a delta."""

    arm: int
    joint: int
    target_deg: Optional[float] = None
    delta_deg: Optional[float] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "arm", validate_arm(self.arm))
        object.__setattr__(self, "joint", validate_joint(self.joint))
        if (self.target_deg is None) == (self.delta_deg is None):
            raise ValidationError("exactly one of target_deg or delta_deg is required")
        if self.target_deg is not None:
            object.__setattr__(self, "target_deg", _finite(self.target_deg, "target_deg"))
        else:
            object.__setattr__(self, "delta_deg", _finite(self.delta_deg, "delta_deg"))

    @classmethod
    def target(cls, arm: Any, joint: Any, target_deg: Any) -> "JointGoal":
        return cls(arm=validate_arm(arm), joint=validate_joint(joint),
                   target_deg=_finite(target_deg, "target_deg"))

    @classmethod
    def delta(cls, arm: Any, joint: Any, delta_deg: Any) -> "JointGoal":
        return cls(arm=validate_arm(arm), joint=validate_joint(joint),
                   delta_deg=_finite(delta_deg, "delta_deg"))


@dataclass(frozen=True)
class CommandPlan:
    commands: Tuple[ArmCommand, ...]
    total_deg_by_joint: Mapping[Tuple[int, int], float]
    start_pose_by_arm: Mapping[int, Tuple[float, ...]]
    end_pose_by_arm: Mapping[int, Tuple[float, ...]]

    @property
    def command_count(self) -> int:
        return len(self.commands)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "commands": [c.as_dict() for c in self.commands],
            "total_deg_by_joint": {"%d:%d" % k: v
                                   for k, v in self.total_deg_by_joint.items()},
            "start_pose_by_arm": {str(k): list(v)
                                  for k, v in self.start_pose_by_arm.items()},
            "end_pose_by_arm": {str(k): list(v)
                                for k, v in self.end_pose_by_arm.items()},
        }


def _validate_pose(pose: Sequence[float]) -> Tuple[float, ...]:
    if len(pose) != 6:
        raise ValidationError("a pose must have 6 joints, got %d" % len(pose))
    return tuple(_finite(v, "joint angle") for v in pose)


def plan_joint_move(
    arm: Any,
    joint: Any,
    current_deg: Any,
    target_deg: Any,
    budget: PlanBudget = PlanBudget(),
) -> CommandPlan:
    """Plan one joint from ``current_deg`` to ``target_deg`` in <=cap steps."""
    arm_i = validate_arm(arm)
    joint_i = validate_joint(joint)
    current = _finite(current_deg, "current_deg")
    target = _finite(target_deg, "target_deg")
    if not _target_in_limits(joint_i, current, target):
        lo, hi = JOINT_LIMITS_DEG[joint_i]
        raise ValidationError(
            "joint %d target %.3f is outside %.1f..%.1f and is not moving "
            "an out-of-range pose back inside" % (joint_i, target, lo, hi)
        )
    delta = target - current
    if abs(delta) > budget.max_total_deg_per_joint + 1e-9:
        raise ValidationError(
            "joint %d travel %.3f exceeds %.1f deg"
            % (joint_i, abs(delta), budget.max_total_deg_per_joint)
        )
    pieces = split_delta(delta, budget.max_step_deg)
    if len(pieces) > budget.max_commands:
        raise ValidationError(
            "joint %d needs %d commands, cap is %d"
            % (joint_i, len(pieces), budget.max_commands)
        )
    commands = tuple(ArmCommand(arm_i, joint_i, p) for p in pieces)
    start = [0.0] * 6
    start[joint_i] = current
    end = list(start)
    end[joint_i] = target
    return CommandPlan(
        commands=commands,
        total_deg_by_joint={(arm_i, joint_i): round(delta, 6)},
        start_pose_by_arm={arm_i: tuple(start)},
        end_pose_by_arm={arm_i: tuple(end)},
    )


def plan_joint_sequence(
    start_pose_by_arm: Mapping[Any, Sequence[float]],
    goals: Iterable[JointGoal],
    budget: PlanBudget = PlanBudget(),
) -> CommandPlan:
    """Plan a multi-joint sequence (J1..J4 extension) from the current pose.

    ``start_pose_by_arm`` maps each arm to its six measured angles.  Goals are
    executed in the order given: a caller that wants a proximal-first extension
    supplies them J1, J2, J3, J4.  The virtual pose is updated after every goal,
    so a later ``target_deg`` is evaluated against the pose the arm will
    actually be in.
    """
    if not isinstance(start_pose_by_arm, Mapping):
        raise ValidationError("start_pose_by_arm must be a mapping of arm -> pose")
    poses: Dict[int, List[float]] = {}
    for arm, pose in start_pose_by_arm.items():
        arm_i = validate_arm(arm)
        poses[arm_i] = list(_validate_pose(pose))
    if not poses:
        raise ValidationError("at least one arm pose is required")

    start_snapshot = {k: tuple(v) for k, v in poses.items()}
    commands: List[ArmCommand] = []
    totals: Dict[Tuple[int, int], float] = {}

    for goal in goals:
        if not isinstance(goal, JointGoal):
            raise ValidationError("goals must be JointGoal instances")
        if goal.arm not in poses:
            raise ValidationError(
                "no start pose for arm %d; supply all six measured angles"
                % goal.arm
            )
        pose = poses[goal.arm]
        current = pose[goal.joint]
        target = goal.target_deg if goal.target_deg is not None \
            else current + float(goal.delta_deg)
        target = _finite(target, "target_deg")
        if not _target_in_limits(goal.joint, current, target):
            lo, hi = JOINT_LIMITS_DEG[goal.joint]
            raise ValidationError(
                "arm %d joint %d target %.3f is outside %.1f..%.1f"
                % (goal.arm, goal.joint, target, lo, hi)
            )
        key = (goal.arm, goal.joint)
        travelled = totals.get(key, 0.0)
        leg = target - current
        if abs(travelled + leg) > budget.max_total_deg_per_joint + 1e-9:
            raise ValidationError(
                "arm %d joint %d total travel %.3f exceeds %.1f deg"
                % (goal.arm, goal.joint, abs(travelled + leg),
                   budget.max_total_deg_per_joint)
            )
        pieces = split_delta(leg, budget.max_step_deg)
        if len(commands) + len(pieces) > budget.max_commands:
            raise ValidationError(
                "sequence needs more than %d commands" % budget.max_commands
            )
        commands.extend(ArmCommand(goal.arm, goal.joint, p) for p in pieces)
        totals[key] = travelled + leg
        pose[goal.joint] = target

    return CommandPlan(
        commands=tuple(commands),
        total_deg_by_joint={k: round(v, 6) for k, v in totals.items()},
        start_pose_by_arm=start_snapshot,
        end_pose_by_arm={k: tuple(v) for k, v in poses.items()},
    )


# ---------------------------------------------------------------------------
# status snapshots
# ---------------------------------------------------------------------------


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    out = float(value)
    return out if math.isfinite(out) else None


@dataclass(frozen=True)
class ArmSnapshot:
    index: int
    side: str
    joints: Tuple[float, ...]
    targets: Tuple[float, ...]
    faults: Tuple[str, ...]
    feedback_age_s: Optional[float]
    connected: bool
    feedback_ok: bool
    feedback_fault: bool
    enabled: bool
    enabled_all: bool
    gripper_deg: float
    gripper_target_deg: float

    @property
    def lag_deg(self) -> float:
        return max(abs(t - m) for t, m in zip(self.targets, self.joints))

    def summary(self) -> Dict[str, Any]:
        return {
            "arm": self.index,
            "side": self.side,
            "joints_deg": [round(v, 3) for v in self.joints],
            "targets_deg": [round(v, 3) for v in self.targets],
            "faults": list(self.faults),
            "feedback_age_s": self.feedback_age_s,
            "connected": self.connected,
            "feedback_ok": self.feedback_ok,
            "enabled": self.enabled,
            "enabled_all": self.enabled_all,
            "gripper_deg": round(self.gripper_deg, 3),
            "gripper_target_deg": round(self.gripper_target_deg, 3),
        }


def arm_snapshot(status: Any, arm: Any) -> ArmSnapshot:
    """Extract one arm and refuse a missing/misrouted side."""
    arm_i = validate_arm(arm)
    if not isinstance(status, Mapping):
        raise SafetyAbort("STATUS_INVALID", "status is not an object")
    if status.get("ok") is False:
        raise SafetyAbort("STATUS_INVALID", "server reported ok=false")
    arms = status.get("arms")
    if not isinstance(arms, list) or len(arms) < 2:
        raise SafetyAbort("STATUS_INVALID", "status.arms must contain both arms")
    raw = arms[arm_i]
    if not isinstance(raw, Mapping):
        raise SafetyAbort("STATUS_INVALID", "arm %d is not an object" % arm_i)
    expected = ARM_SIDES[arm_i]
    side = raw.get("side")
    if side is not None and side != expected:
        raise SafetyAbort(
            "ARM_ROUTE",
            "arm %d should be %s but the server reports %r" % (arm_i, expected, side),
        )
    joints = raw.get("joints_deg")
    targets = raw.get("targets_deg")
    if not isinstance(joints, list) or len(joints) != 6:
        raise SafetyAbort("STATUS_INVALID", "joints_deg must have 6 values")
    if not isinstance(targets, list) or len(targets) != 6:
        raise SafetyAbort("STATUS_INVALID", "targets_deg must have 6 values")
    joint_values = tuple(_number(v) for v in joints)
    target_values = tuple(_number(v) for v in targets)
    if any(v is None for v in joint_values) or any(v is None for v in target_values):
        raise SafetyAbort("FEEDBACK_INVALID", "joint or target angles are not finite")

    raw_faults = raw.get("faults") or []
    if not isinstance(raw_faults, list):
        raise SafetyAbort("STATUS_INVALID", "faults must be a list")
    faults: List[str] = []
    for item in raw_faults:
        if isinstance(item, (list, tuple)):
            faults.append(str(item[-1]) if item else str(item))
        elif item:
            faults.append(str(item))

    snapshot = ArmSnapshot(
        index=arm_i,
        side=str(side if side is not None else expected),
        joints=joint_values,  # type: ignore[arg-type]
        targets=target_values,  # type: ignore[arg-type]
        faults=tuple(faults),
        feedback_age_s=_number(raw.get("feedback_age_s")),
        connected=raw.get("connected") is True,
        feedback_ok=raw.get("feedback_ok") is True,
        feedback_fault=raw.get("feedback_fault") is True,
        enabled=raw.get("enabled") is True,
        enabled_all=raw.get("enabled_all") is True,
        gripper_deg=0.0,
        gripper_target_deg=0.0,
    )
    measured_grip = _number(raw.get("gripper_deg"))
    target_grip = _number(raw.get("gripper_target_deg"))
    if measured_grip is None or target_grip is None:
        raise SafetyAbort("FEEDBACK_INVALID",
                          "arm %d has no finite gripper feedback" % arm_i)
    return replace(snapshot, gripper_deg=measured_grip,
                   gripper_target_deg=target_grip)


# ---------------------------------------------------------------------------
# transport
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Frame:
    cam: int
    data: bytes
    fetched_at: float
    sha256: str

    def ref(self) -> Dict[str, Any]:
        return {"cam": self.cam, "fetched_at": round(self.fetched_at, 3),
                "sha256": self.sha256, "bytes": len(self.data)}


class Transport:
    """The small surface the state machine is allowed to touch."""

    def status(self) -> Dict[str, Any]:
        raise NotImplementedError

    def arm(self, command: ArmCommand) -> Dict[str, Any]:
        raise NotImplementedError

    def gripper(self, command: GripperCommand) -> Dict[str, Any]:
        raise NotImplementedError

    def capture(self, cam: int) -> Frame:
        raise NotImplementedError

    def heartbeat(self) -> None:
        raise NotImplementedError

    def lease_valid(self) -> bool:
        raise NotImplementedError

    def safe_stop(self) -> bool:
        raise NotImplementedError

    def stop_recording(self, success: bool = False) -> bool:
        raise NotImplementedError

    def release(self) -> bool:
        raise NotImplementedError


class SimulatedTransport(Transport):
    """Deterministic stand-in for dry runs and unit tests.

    It is not a robot and it cannot produce a grasp success on its own: the
    controller's dry-run flag still suppresses success.
    """

    def __init__(
        self,
        start_joints: Sequence[Sequence[float]] = ((0.0,) * 6, (0.0,) * 6),
        gripper_deg: Sequence[float] = (35.0, 35.0),
        contact_at_deg: float = 18.0,
        clock: Callable[[], float] = time.time,
        stale_after_commands: Optional[int] = None,
        fault_after_commands: Optional[int] = None,
        lag_after_commands: Optional[int] = None,
        lease_lost_after_commands: Optional[int] = None,
        clamp_joint: Optional[int] = None,
        reject_after_commands: Optional[int] = None,
        measured_stuck: bool = False,
        image_age_s: float = 0.0,
        estop: bool = False,
    ):
        if len(start_joints) != 2:
            raise ValidationError("start_joints needs two poses")
        self.joints = [list(_validate_pose(p)) for p in start_joints]
        self.targets = [list(p) for p in self.joints]
        self.gripper_deg = [float(g) for g in gripper_deg]
        if len(self.gripper_deg) != 2:
            raise ValidationError("gripper_deg needs two values")
        self.gripper_target = list(self.gripper_deg)
        self.contact_at = float(contact_at_deg)
        self.clock = clock
        self.stale_after_commands = stale_after_commands
        self.fault_after_commands = fault_after_commands
        self.lag_after_commands = lag_after_commands
        self.lease_lost_after_commands = lease_lost_after_commands
        self.clamp_joint = clamp_joint
        self.reject_after_commands = reject_after_commands
        self.measured_stuck = measured_stuck
        self.image_age_s = float(image_age_s)
        self.estop = bool(estop)
        self.arm_commands: List[ArmCommand] = []
        self.gripper_commands: List[GripperCommand] = []
        self.heartbeats = 0
        self.safe_stops = 0
        self.record_stops = 0
        self.record_success: List[bool] = []
        self.releases = 0
        self.faults: List[List[Any]] = [[], []]
        self.feedback_age = [0.05, 0.05]
        self.feedback_ok_flags = [True, True]
        self.forced_lag = False
        self.lease_ok = True
        self._seq = 0

    def _maybe_event(self) -> None:
        self._seq += 1
        if self.fault_after_commands is not None and \
                self._seq > self.fault_after_commands:
            self.faults[0] = [("driver_error", "injected fault")]
        if self.lag_after_commands is not None and \
                self._seq > self.lag_after_commands:
            self.forced_lag = True
            self.joints[0][0] = self.targets[0][0] - 40.0
        if self.lease_lost_after_commands is not None and \
                self._seq > self.lease_lost_after_commands:
            self.lease_ok = False
        if self.stale_after_commands is not None and \
                self._seq > self.stale_after_commands:
            self.feedback_age = [99.0, 99.0]

    def status(self) -> Dict[str, Any]:
        arms = []
        for i in range(2):
            arms.append({
                "side": ARM_SIDES[i],
                "connected": True,
                "feedback_ok": self.feedback_ok_flags[i],
                "feedback_fault": False,
                "feedback_age_s": self.feedback_age[i],
                "enabled": True,
                "enabled_all": True,
                "joints_deg": list(self.joints[i]),
                "targets_deg": list(self.targets[i]),
                "faults": [list(f) if isinstance(f, (list, tuple)) else f
                           for f in self.faults[i]],
                "gripper_deg": self.gripper_deg[i],
                "gripper_target_deg": self.gripper_target[i],
            })
        return {"ok": True, "estop": self.estop, "arms": arms,
                "limits": {"max_joint_step_deg": MAX_API_STEP_DEG}}

    def arm(self, command: ArmCommand) -> Dict[str, Any]:
        self.arm_commands.append(command)
        self._maybe_event()
        if self.reject_after_commands is not None and \
                len(self.arm_commands) > self.reject_after_commands:
            return {"ok": False, "error": "injected rejection"}
        i, j = command.arm, command.joint
        self.targets[i][j] = round(self.targets[i][j] + command.delta_deg, 6)
        if self.clamp_joint == j:
            self.targets[i][j] = round(self.targets[i][j] - command.delta_deg, 6)
        if not self.measured_stuck and not self.forced_lag:
            self.joints[i][j] = self.targets[i][j]
        return {"ok": True, "targets_deg": list(self.targets[i])}

    def gripper(self, command: GripperCommand) -> Dict[str, Any]:
        self.gripper_commands.append(command)
        self._maybe_event()
        i = command.arm
        self.gripper_target[i] = max(
            0.0, min(GRIPPER_OPEN_DEG, self.gripper_target[i] + command.delta_deg)
        )
        if command.delta_deg < 0.0:
            self.gripper_deg[i] = max(
                self.contact_at, self.gripper_deg[i] + command.delta_deg
            )
        else:
            self.gripper_deg[i] = min(
                GRIPPER_OPEN_DEG, self.gripper_deg[i] + command.delta_deg
            )
        return {"ok": True, "gripper_target_deg": self.gripper_target[i]}

    def capture(self, cam: int) -> Frame:
        now = self.clock()
        data = (b"\xff\xd8SIMULATED-CAM%d" % int(cam)) + b"\xff\xd9"
        return Frame(cam=int(cam), data=data, fetched_at=now - self.image_age_s,
                     sha256=hashlib.sha256(data).hexdigest())

    def heartbeat(self) -> None:
        self.heartbeats += 1

    def lease_valid(self) -> bool:
        return self.lease_ok

    def safe_stop(self) -> bool:
        self.safe_stops += 1
        return True

    def stop_recording(self, success: bool = False) -> bool:
        self.record_stops += 1
        self.record_success.append(bool(success))
        return True

    def release(self) -> bool:
        self.releases += 1
        return True


def _load_lease_session_class():
    """Reuse ``tools/run-smolvla.py``'s battle-tested LeaseSession."""
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, "run-smolvla.py")
    spec = importlib.util.spec_from_file_location("_agilex_run_smolvla", path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise RuleGraspError("cannot load the lease client from %s" % path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.LeaseSession


class HttpTransport(Transport):
    """Real HTTP transport.  Constructed only for an explicit execute run.

    ``lease`` is injectable for tests and for a caller that already owns a
    lease.  The default builds the same ``LeaseSession`` used by
    ``run-smolvla.py``, including its daemon heartbeat and 409 -> LeaseLost
    conversion.
    """

    def __init__(self, api: str, controller: str,
                 lease: Any = None, timeout: float = 10.0,
                 clock: Callable[[], float] = time.time):
        self.api = api.rstrip("/")
        self.controller = controller
        self.timeout = float(timeout)
        self.clock = clock
        if lease is None:
            lease = _load_lease_session_class()(self.api, controller,
                                                timeout=self.timeout)
        self.lease = lease
        self.last_error: Optional[str] = None

    def acquire(self, takeover: bool = False, confirm: Optional[str] = None) -> bool:
        return bool(self.lease.acquire(takeover=takeover, confirm=confirm))

    def status(self) -> Dict[str, Any]:
        return self.lease.get("/api/status", timeout=self.timeout)

    def arm(self, command: ArmCommand) -> Dict[str, Any]:
        return self.lease.post("/api/arm", command.body(), timeout=self.timeout)

    def gripper(self, command: GripperCommand) -> Dict[str, Any]:
        return self.lease.post("/api/gripper", command.body(), timeout=self.timeout)

    def capture(self, cam: int) -> Frame:
        url = "%s/frame?cam=%d" % (self.api, int(cam))
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=self.timeout) as response:
            data = response.read()
        if not data.startswith(b"\xff\xd8"):
            raise SafetyAbort("IMAGE_INVALID", "cam %d did not return a JPEG" % cam)
        fetched_at = self.clock()
        return Frame(cam=int(cam), data=data, fetched_at=fetched_at,
                     sha256=hashlib.sha256(data).hexdigest())

    def heartbeat(self) -> None:
        self.lease.heartbeat()

    def lease_valid(self) -> bool:
        return bool(not getattr(self.lease, "lost", True) and
                    getattr(self.lease, "secret", None))

    def safe_stop(self) -> bool:
        # Zero velocity is lease-exempt on the server, so a lost lease cannot
        # block the stop.  This is not an E-STOP; it only stops the chassis.
        try:
            response = self.lease.post("/api/drive",
                                       {"lin": 0.0, "ang": 0.0},
                                       timeout=5.0)
            return not isinstance(response, Mapping) or response.get("ok") is not False
        except Exception:
            return False

    def stop_recording(self, success: bool = False) -> bool:
        try:
            response = self.lease.post("/api/record/stop",
                                       {"success": bool(success)},
                                       timeout=5.0)
        except Exception:
            return False
        if isinstance(response, Mapping) and response.get("ok") is False:
            # Not recording is a benign no-op; anything else is a real error.
            return response.get("error") == "not recording"
        return True

    def release(self) -> bool:
        try:
            self.lease.release()
            return True
        except Exception:
            return False


# ---------------------------------------------------------------------------
# vision (advisory input is never success)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BoardObservation:
    detected: bool
    center_x: float
    center_y: float
    width_px: float
    confidence: float
    captured_at: float
    source: str = "probe"

    def as_dict(self) -> Dict[str, Any]:
        return {
            "detected": self.detected,
            "center_x": self.center_x,
            "center_y": self.center_y,
            "width_px": self.width_px,
            "confidence": self.confidence,
            "captured_at": self.captured_at,
            "source": self.source,
        }

    @classmethod
    def from_value(cls, value: Any, captured_at: float) -> "BoardObservation":
        if isinstance(value, BoardObservation):
            return replace(value, captured_at=captured_at)
        if isinstance(value, Mapping):
            return cls(
                detected=bool(value.get("detected")),
                center_x=_finite(value.get("center_x", 0.0), "center_x"),
                center_y=_finite(value.get("center_y", 0.0), "center_y"),
                width_px=_finite(value.get("width_px", 0.0), "width_px"),
                confidence=_finite(value.get("confidence", 0.0), "confidence"),
                captured_at=captured_at,
                source=str(value.get("source") or "probe"),
            )
        raise SafetyAbort("VISION_INVALID", "probe returned %r" % (value,))


@dataclass(frozen=True)
class VerificationConfig:
    min_follow_px: float = 8.0
    min_lift_px: float = 4.0
    min_confidence: float = 0.5
    max_scale_change: float = 0.35

    def __post_init__(self) -> None:
        for name in ("min_follow_px", "min_lift_px", "min_confidence",
                     "max_scale_change"):
            value = _finite(getattr(self, name), name)
            if value < 0.0:
                raise ValidationError("%s must be non-negative" % name)


@dataclass(frozen=True)
class VerificationResult:
    ok: bool
    reason: str
    checks: Mapping[str, Any]


def verify_grasp(
    baseline: Optional[BoardObservation],
    after: Optional[BoardObservation],
    contact: bool,
    frame_fetched_at: Optional[float],
    now: float,
    max_image_age_s: float,
    config: VerificationConfig = VerificationConfig(),
) -> VerificationResult:
    """The only path to ``success``.  HTTP status is not an input."""
    checks: Dict[str, Any] = {}
    checks["contact_candidate"] = bool(contact)
    if not contact:
        return VerificationResult(False, "NO_CONTACT", checks)
    if baseline is None or after is None:
        return VerificationResult(False, "NO_OBSERVATION", checks)
    if frame_fetched_at is None:
        return VerificationResult(False, "NO_IMAGE", checks)
    age = now - float(frame_fetched_at)
    checks["image_age_s"] = round(age, 3)
    if age < -0.5 or age > max_image_age_s:
        return VerificationResult(False, "IMAGE_STALE", checks)
    if not baseline.detected or not after.detected:
        checks["baseline_detected"] = baseline.detected
        checks["after_detected"] = after.detected
        return VerificationResult(False, "BOARD_NOT_VISIBLE", checks)
    obs_age = now - float(after.captured_at)
    checks["observation_age_s"] = round(obs_age, 3)
    if obs_age < -0.5 or obs_age > max_image_age_s:
        return VerificationResult(False, "IMAGE_STALE", checks)
    if after.confidence < config.min_confidence:
        checks["confidence"] = after.confidence
        return VerificationResult(False, "LOW_CONFIDENCE", checks)

    dx = after.center_x - baseline.center_x
    dy = after.center_y - baseline.center_y
    follow = math.hypot(dx, dy)
    lift = baseline.center_y - after.center_y  # image y grows downward
    scale = (after.width_px / baseline.width_px) if baseline.width_px > 0 else 0.0
    checks.update({
        "follow_px": round(follow, 3),
        "lift_px": round(lift, 3),
        "width_ratio": round(scale, 3),
        "baseline": baseline.source,
        "after": after.source,
    })
    if follow < config.min_follow_px:
        return VerificationResult(False, "BOARD_NOT_FOLLOWING", checks)
    if lift < config.min_lift_px:
        return VerificationResult(False, "NO_LIFT", checks)
    if scale <= 0.0 or abs(scale - 1.0) > config.max_scale_change:
        return VerificationResult(False, "SCALE_JUMP", checks)
    return VerificationResult(True, "VERIFIED", checks)


@dataclass(frozen=True)
class Advisory:
    """A model/VLM hint.  Logged, never consulted by rules."""

    source: str
    value: Any
    confidence: float = 0.0
    note: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {"source": self.source, "value": self.value,
                "confidence": self.confidence, "note": self.note}


# ---------------------------------------------------------------------------
# safety configuration and state machine
# ---------------------------------------------------------------------------


class State(str, Enum):
    OBSERVE = "OBSERVE"
    ALIGN = "ALIGN"
    APPROACH = "APPROACH"
    CLOSE = "CLOSE"
    VERIFY = "VERIFY"
    ABORT = "ABORT"


_ALLOWED = {
    State.OBSERVE: {State.ALIGN, State.ABORT},
    State.ALIGN: {State.APPROACH, State.ABORT},
    State.APPROACH: {State.CLOSE, State.ABORT},
    State.CLOSE: {State.VERIFY, State.ABORT},
    State.VERIFY: {State.ABORT},
    State.ABORT: set(),
}


@dataclass(frozen=True)
class RuleConfig:
    max_step_deg: float = MAX_API_STEP_DEG
    max_total_deg_per_joint: float = 45.0
    max_commands: int = 128
    max_plan_duration_s: float = 180.0
    max_lag_deg: float = 6.0
    max_start_lag_deg: float = 1.0
    max_feedback_age_s: float = 1.5
    max_image_age_s: float = 2.0
    settle_timeout_s: float = 4.0
    settle_poll_s: float = 0.2
    settle_tolerance_deg: float = 0.8
    heartbeat_interval_s: float = 5.0
    people_clear_ttl_s: float = 900.0
    require_enabled_all: bool = True
    gripper_step_deg: float = 5.0
    gripper_max_segments: int = 15
    gripper_settle_s: float = 0.4
    contact_min_lag_deg: float = 2.0
    contact_samples: int = 2
    verify: VerificationConfig = field(default_factory=VerificationConfig)

    def __post_init__(self) -> None:
        if not 0.0 < _finite(self.max_step_deg, "max_step_deg") <= MAX_API_STEP_DEG + 1e-9:
            raise ValidationError("max_step_deg must be in (0, 2.0]")
        for name in ("max_total_deg_per_joint", "max_plan_duration_s",
                     "max_feedback_age_s", "max_image_age_s",
                     "settle_timeout_s", "heartbeat_interval_s",
                     "people_clear_ttl_s", "gripper_step_deg",
                     "gripper_settle_s", "contact_min_lag_deg"):
            if _finite(getattr(self, name), name) <= 0.0:
                raise ValidationError("%s must be positive" % name)
        lag = _finite(self.max_lag_deg, "max_lag_deg")
        if not 0.0 <= lag <= 15.0:
            raise ValidationError("max_lag_deg must be in 0..15 (server cap)")
        start_lag = _finite(self.max_start_lag_deg, "max_start_lag_deg")
        if not 0.0 <= start_lag <= lag:
            raise ValidationError("max_start_lag_deg must be in 0..max_lag_deg")
        if _finite(self.max_feedback_age_s, "max_feedback_age_s") > 2.0:
            raise ValidationError("max_feedback_age_s must be <= 2.0 s")
        if _finite(self.heartbeat_interval_s, "heartbeat_interval_s") > 10.0:
            raise ValidationError("heartbeat_interval_s must be <= 10 s (lease TTL 20 s)")
        if not 0.0 <= _finite(self.settle_tolerance_deg,
                              "settle_tolerance_deg"):
            raise ValidationError("settle_tolerance_deg must be non-negative")
        if _finite(self.settle_poll_s, "settle_poll_s") <= 0.0:
            raise ValidationError("settle_poll_s must be positive")
        for name in ("max_commands", "gripper_max_segments", "contact_samples"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValidationError("%s must be a positive integer" % name)
        if self.gripper_step_deg > 10.0:
            raise ValidationError("gripper_step_deg must be <= 10 deg")


@dataclass
class RunResult:
    success: bool
    state: str
    reason: str
    commands: int
    contact: bool
    verified: bool
    dry_run: bool
    checks: Dict[str, Any] = field(default_factory=dict)
    events: List[Dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "state": self.state,
            "reason": self.reason,
            "commands": self.commands,
            "contact": self.contact,
            "verified": self.verified,
            "dry_run": self.dry_run,
            "checks": self.checks,
            "events": len(self.events),
        }


class EventLog:
    """Append-only JSONL.  Never writes images or secrets, only references."""

    def __init__(self, path: Optional[str], run_id: str,
                 clock: Callable[[], float] = time.time):
        self.path = path
        self.run_id = run_id
        self.clock = clock
        self.events: List[Dict[str, Any]] = []
        self._seq = 0
        if path:
            parent = os.path.dirname(os.path.abspath(path))
            os.makedirs(parent, exist_ok=True)

    def emit(self, event: str, **fields: Any) -> Dict[str, Any]:
        self._seq += 1
        record = {
            "seq": self._seq,
            "ts": round(self.clock(), 3),
            "run_id": self.run_id,
            "event": event,
        }
        record.update(fields)
        self.events.append(record)
        if self.path:
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, sort_keys=True,
                                        ensure_ascii=False) + "\n")
        return record


def _status_summary(status: Mapping[str, Any]) -> Dict[str, Any]:
    out = {"ok": status.get("ok"), "estop": status.get("estop")}
    arms = []
    for arm in status.get("arms") or []:
        if not isinstance(arm, Mapping):
            continue
        arms.append({
            "side": arm.get("side"),
            "joints_deg": arm.get("joints_deg"),
            "targets_deg": arm.get("targets_deg"),
            "faults": arm.get("faults"),
            "feedback_age_s": arm.get("feedback_age_s"),
            "connected": arm.get("connected"),
            "feedback_ok": arm.get("feedback_ok"),
            "enabled": arm.get("enabled"),
            "enabled_all": arm.get("enabled_all"),
            "gripper_deg": arm.get("gripper_deg"),
            "gripper_target_deg": arm.get("gripper_target_deg"),
        })
    out["arms"] = arms
    return out


class RuleGraspController:
    """Fail-closed OBSERVE -> ... -> VERIFY state machine."""

    def __init__(
        self,
        transport: Transport,
        config: RuleConfig = RuleConfig(),
        log: Optional[EventLog] = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        people_clear: bool = False,
        people_cleared_at: Optional[float] = None,
        vision_probe: Optional[Callable[[Frame], Any]] = None,
        vision_cam: int = 4,
        dry_run: bool = True,
        run_id: Optional[str] = None,
    ):
        self.transport = transport
        self.config = config
        self.clock = clock
        self.sleep = sleep
        self.people_clear = bool(people_clear)
        self.people_cleared_at = (
            clock() if people_cleared_at is None else float(people_cleared_at)
        )
        self.vision_probe = vision_probe
        self.vision_cam = int(vision_cam)
        self.dry_run = bool(dry_run)
        self.run_id = run_id or "rule-grasp-%d" % int(clock())
        self.log = log or EventLog(None, self.run_id, clock)
        self.advisories: List[Advisory] = []
        self._state = State.OBSERVE
        self._commands = 0
        self._started_at: Optional[float] = None
        self._last_heartbeat = 0.0
        self._travel: Dict[Tuple[int, int], float] = {}
        self._verified_success = False

    @property
    def state(self) -> State:
        return self._state

    def add_advisory(self, advisory: Advisory) -> None:
        """Record a model/VLM hint.  It cannot reach a decision."""
        self.advisories.append(advisory)
        self.log.emit("advisory", advisory=advisory.as_dict())

    def plan(self, plan_doc: Mapping[str, Any],
             budget: Optional[PlanBudget] = None) -> Tuple[CommandPlan, CommandPlan]:
        """Build ALIGN and APPROACH plans from a JSON-shaped document."""
        if not isinstance(plan_doc, Mapping):
            raise ValidationError("plan must be an object")
        budget = budget or PlanBudget(
            max_step_deg=self.config.max_step_deg,
            max_total_deg_per_joint=self.config.max_total_deg_per_joint,
            max_commands=self.config.max_commands,
            max_duration_s=self.config.max_plan_duration_s,
        )
        status = self.transport.status()
        arms = status.get("arms") if isinstance(status, Mapping) else None
        if not isinstance(arms, list) or len(arms) < 2:
            raise SafetyAbort("STATUS_INVALID", "cannot plan without both arms")
        start = {}
        for i in (0, 1):
            raw = arms[i]
            if not isinstance(raw, Mapping) or not isinstance(raw.get("joints_deg"), list):
                raise SafetyAbort("STATUS_INVALID", "arm %d has no joints_deg" % i)
            start[i] = _validate_pose(raw["joints_deg"])

        default_arm = plan_doc.get("arm", 0)
        validate_arm(default_arm)

        align_goals = self._parse_goals(plan_doc.get("align") or [], default_arm)
        approach_goals = self._parse_goals(plan_doc.get("approach") or [], default_arm)

        # Refuse to build a measured-pose plan while the held target is far
        # from the measured pose; the relative API would offset every command.
        for index in sorted({g.arm for g in align_goals + approach_goals}):
            snap = arm_snapshot(status, index)
            if snap.lag_deg > self.config.max_start_lag_deg:
                raise SafetyAbort(
                    "TARGET_NOT_ALIGNED",
                    "arm %d target lags measured by %.2f deg before planning"
                    % (index, snap.lag_deg),
                )

        align_plan = plan_joint_sequence(start, align_goals, budget) if align_goals \
            else CommandPlan((), {}, {k: tuple(v) for k, v in start.items()},
                             {k: tuple(v) for k, v in start.items()})
        approach_start = {k: list(v) for k, v in align_plan.end_pose_by_arm.items()}
        for k, pose in start.items():
            approach_start.setdefault(k, list(pose))
        approach_plan = plan_joint_sequence(approach_start, approach_goals, budget) \
            if approach_goals else CommandPlan(
                (), {}, {k: tuple(v) for k, v in approach_start.items()},
                {k: tuple(v) for k, v in approach_start.items()})

        self.log.emit("plan",
                      align=align_plan.as_dict(),
                      approach=approach_plan.as_dict(),
                      budget={
                          "max_step_deg": budget.max_step_deg,
                          "max_total_deg_per_joint": budget.max_total_deg_per_joint,
                          "max_commands": budget.max_commands,
                          "max_duration_s": budget.max_duration_s,
                      })
        return align_plan, approach_plan

    def _parse_goals(self, items: Any, default_arm: int) -> List[JointGoal]:
        if not isinstance(items, list):
            raise ValidationError("align/approach must be lists")
        goals: List[JointGoal] = []
        for item in items:
            if not isinstance(item, Mapping):
                raise ValidationError("each move must be an object")
            arm = item.get("arm", default_arm)
            joint = parse_joint_ref(item.get("joint"))
            has_target = item.get("target_deg") is not None
            has_delta = item.get("delta_deg") is not None
            if has_target == has_delta:
                raise ValidationError(
                    "move for joint %s needs exactly one of target_deg/delta_deg"
                    % joint
                )
            if has_target:
                goals.append(JointGoal.target(arm, joint, item["target_deg"]))
            else:
                goals.append(JointGoal.delta(arm, joint, item["delta_deg"]))
        return goals

    def run(self, align_plan: CommandPlan, approach_plan: CommandPlan,
            gripper_arm: Any = 0) -> RunResult:
        gripper_arm = validate_arm(gripper_arm)
        self._started_at = self.clock()
        self._last_heartbeat = self._started_at
        self._commands = 0
        self._travel.clear()
        self._verified_success = False
        self.log.emit("run_start", dry_run=self.dry_run, state=self._state.value,
                      config=self._public_config(), people_clear=self.people_clear,
                      run_id=self.run_id)
        success = False
        contact = False
        verified = False
        reason = "ABORTED"
        checks: Dict[str, Any] = {}
        try:
            active = list(_plan_arms(align_plan, approach_plan))
            if gripper_arm not in active:
                active.append(gripper_arm)
            self._observe(active)
            self._transition(State.ALIGN)
            self._execute_plan(align_plan)

            self._transition(State.APPROACH)
            self._execute_plan(approach_plan)
            response = self._check_safety("approach_end")
            self._check_active_arms(response, _plan_arms(align_plan, approach_plan))

            self._transition(State.CLOSE)
            baseline_frame, baseline_obs = self._observe_board("baseline")
            contact, close_reason = self._close_gripper(gripper_arm)
            checks["close_reason"] = close_reason

            self._transition(State.VERIFY)
            after_frame, after_obs = self._observe_board("after")
            if self.dry_run:
                verified = False
                reason = "DRY_RUN_NOT_VERIFIED"
                checks["verification"] = "skipped in dry run"
                self.log.emit("verify", ok=False, reason=reason,
                              baseline=baseline_obs.as_dict() if baseline_obs else None,
                              after=after_obs.as_dict() if after_obs else None)
            else:
                if self.vision_probe is None:
                    raise SafetyAbort("NO_VISION", "execute mode needs a vision probe")
                verification = verify_grasp(
                    baseline_obs, after_obs, contact,
                    after_frame.fetched_at if after_frame else None,
                    self.clock(), self.config.max_image_age_s,
                    self.config.verify,
                )
                checks["verification"] = verification.reason
                checks.update(verification.checks)
                self.log.emit("verify", ok=verification.ok,
                              reason=verification.reason,
                              checks=dict(verification.checks),
                              baseline=baseline_obs.as_dict() if baseline_obs else None,
                              after=after_obs.as_dict() if after_obs else None,
                              image=after_frame.ref() if after_frame else None)
                if not verification.ok:
                    raise SafetyAbort("VERIFY_FAILED", verification.reason)
                verified = True
                success = True
                reason = "VERIFIED"
        except KeyboardInterrupt:
            success = False
            verified = False
            reason = "INTERRUPTED"
            checks["abort_detail"] = "operator interrupt"
            self._abort(SafetyAbort("INTERRUPTED", "operator interrupt"))
        except SafetyAbort as exc:
            success = False
            verified = False
            reason = exc.code
            checks["abort_detail"] = exc.detail
            self._abort(exc)
        except (ValidationError, RuleGraspError) as exc:
            success = False
            verified = False
            reason = "ERROR"
            checks["error"] = str(exc)
            self._abort(SafetyAbort("ERROR", str(exc)))
        except Exception as exc:  # transport/socket failures must not look like success
            success = False
            verified = False
            reason = "TRANSPORT_ERROR"
            checks["error"] = "%s: %s" % (type(exc).__name__, exc)
            self._abort(SafetyAbort("TRANSPORT_ERROR", str(exc)))
        finally:
            self._verified_success = bool(success and not self.dry_run)
            self._cleanup()

        result = RunResult(
            success=bool(success and not self.dry_run),
            state=self._state.value,
            reason=reason,
            commands=self._commands,
            contact=bool(contact),
            verified=bool(verified),
            dry_run=self.dry_run,
            checks=checks,
            events=list(self.log.events),
        )
        self.log.emit("run_end", success=result.success, state=result.state,
                      reason=result.reason, commands=result.commands,
                      contact=result.contact, verified=result.verified,
                      dry_run=result.dry_run, checks=result.checks,
                      advisories=[a.as_dict() for a in self.advisories])
        result.events = list(self.log.events)
        return result

    def _transition(self, new: State) -> None:
        if new not in _ALLOWED[self._state]:
            raise SafetyAbort(
                "BAD_TRANSITION",
                "%s -> %s is not allowed" % (self._state.value, new.value),
            )
        old = self._state
        self._state = new
        self.log.emit("state", old=old.value, new=new.value)

    def _abort(self, exc: SafetyAbort) -> None:
        old = self._state
        self._state = State.ABORT
        self.log.emit("abort", old=old.value, code=exc.code, detail=exc.detail)

    def _public_config(self) -> Dict[str, Any]:
        return {
            "max_step_deg": self.config.max_step_deg,
            "max_total_deg_per_joint": self.config.max_total_deg_per_joint,
            "max_lag_deg": self.config.max_lag_deg,
            "max_start_lag_deg": self.config.max_start_lag_deg,
            "max_feedback_age_s": self.config.max_feedback_age_s,
            "max_image_age_s": self.config.max_image_age_s,
            "max_plan_duration_s": self.config.max_plan_duration_s,
            "people_clear_ttl_s": self.config.people_clear_ttl_s,
        }

    def _check_people(self, now: float) -> None:
        if not self.people_clear:
            raise SafetyAbort("PEOPLE_NOT_CLEAR",
                              "operator has not confirmed the area is clear")
        if now - self.people_cleared_at > self.config.people_clear_ttl_s:
            raise SafetyAbort(
                "PEOPLE_CLEAR_EXPIRED",
                "area clearance is older than %.0f s" % self.config.people_clear_ttl_s,
            )

    def _check_safety(self, stage: str, active_arms: Sequence[int] = ()) -> Dict[str, Any]:
        now = self.clock()
        self._check_people(now)
        if not self.transport.lease_valid():
            raise SafetyAbort("LEASE_LOST", "control lease is not valid")
        if self._started_at is not None and \
                now - self._started_at > self.config.max_plan_duration_s:
            raise SafetyAbort("PLAN_TIMEOUT",
                              "plan exceeded %.0f s" % self.config.max_plan_duration_s)
        status = self.transport.status()
        if not isinstance(status, Mapping):
            raise SafetyAbort("STATUS_INVALID", "status is not an object")
        if status.get("estop") is True:
            raise SafetyAbort("ESTOP", "server reports E-STOP active")
        arms = status.get("arms")
        if not isinstance(arms, list) or len(arms) < 2:
            raise SafetyAbort("STATUS_INVALID", "status.arms must contain both arms")
        checked = set(active_arms) if active_arms else {0, 1}
        for index in range(2):
            snap = arm_snapshot(status, index)
            if snap.faults:
                raise SafetyAbort("FAULT", "arm %d faults: %s"
                                  % (index, ", ".join(snap.faults)))
            # Stale feedback and target lag are checked on BOTH arms: an idle
            # arm that is drifting is still a hazard the moment it is used.
            if not snap.connected or not snap.feedback_ok or snap.feedback_fault:
                raise SafetyAbort("FEEDBACK_LOST",
                                  "arm %d feedback is not healthy" % index)
            if snap.feedback_age_s is None:
                raise SafetyAbort("FEEDBACK_STALE",
                                  "arm %d has no feedback age" % index)
            if snap.feedback_age_s < -0.5 or                     snap.feedback_age_s > self.config.max_feedback_age_s:
                raise SafetyAbort(
                    "FEEDBACK_STALE",
                    "arm %d feedback is %.2f s old" % (index, snap.feedback_age_s),
                )
            if snap.lag_deg > self.config.max_lag_deg:
                raise SafetyAbort(
                    "TARGET_LAG",
                    "arm %d target lag %.2f deg > %.2f"
                    % (index, snap.lag_deg, self.config.max_lag_deg),
                )
            if index in checked:
                enabled = snap.enabled_all if self.config.require_enabled_all else snap.enabled
                if not enabled:
                    raise SafetyAbort("DISABLED",
                                      "arm %d is not fully enabled" % index)
        self._maybe_heartbeat(now)
        self.log.emit("safety_ok", stage=stage, active_arms=list(checked),
                      status=_status_summary(status))
        return status

    def _check_active_arms(self, status: Mapping[str, Any],
                           active_arms: Sequence[int]) -> None:
        for index in active_arms:
            snap = arm_snapshot(status, index)
            if snap.lag_deg > self.config.max_lag_deg:
                raise SafetyAbort(
                    "TARGET_LAG",
                    "arm %d lag %.2f after motion" % (index, snap.lag_deg),
                )

    def _maybe_heartbeat(self, now: float) -> None:
        if self.dry_run:
            return
        if now - self._last_heartbeat >= self.config.heartbeat_interval_s:
            try:
                self.transport.heartbeat()
            except Exception as exc:
                raise SafetyAbort("LEASE_LOST", "heartbeat failed: %s" % exc)
            self._last_heartbeat = now
            self.log.emit("lease_heartbeat", at=round(now, 3))

    def _capture(self, label: str) -> Frame:
        try:
            frame = self.transport.capture(self.vision_cam)
        except SafetyAbort:
            raise
        except Exception as exc:
            raise SafetyAbort("IMAGE_UNAVAILABLE", str(exc))
        now = self.clock()
        age = now - frame.fetched_at
        if age < -0.5 or age > self.config.max_image_age_s:
            raise SafetyAbort(
                "IMAGE_STALE",
                "%s image is %.2f s old (max %.2f)"
                % (label, age, self.config.max_image_age_s),
            )
        self.log.emit("image", label=label, image=frame.ref())
        return frame

    def _observe(self, active_arms: Sequence[int] = ()) -> Dict[str, Any]:
        status = self._check_safety("observe", active_arms)
        # /api/arm moves the held target, not the measured pose.  A plan built
        # from joints_deg is only equivalent when target == measured, so a
        # start-of-run lag is refused instead of silently offsetting the run.
        for index in sorted(set(active_arms) or {0, 1}):
            snap = arm_snapshot(status, index)
            if snap.lag_deg > self.config.max_start_lag_deg:
                raise SafetyAbort(
                    "TARGET_NOT_ALIGNED",
                    "arm %d target lags measured by %.2f deg at start"
                    % (index, snap.lag_deg),
                )
        self._capture("observe")
        for index in (0, 1):
            snap = arm_snapshot(status, index)
            self.log.emit("observe_arm", arm=snap.summary())
        return status

    def _execute_plan(self, plan: CommandPlan) -> None:
        for command in plan.commands:
            self._execute_command(command)

    def _execute_command(self, command: ArmCommand) -> None:
        now = self.clock()
        if self._started_at is None:
            self._started_at = now
        if now - self._started_at > self.config.max_plan_duration_s:
            raise SafetyAbort("PLAN_TIMEOUT", "plan duration exceeded")
        if self._commands >= self.config.max_commands:
            raise SafetyAbort("COMMAND_LIMIT",
                              "run exceeded %d commands" % self.config.max_commands)
        before_status = self._check_safety("before_command", [command.arm])
        before_snap = arm_snapshot(before_status, command.arm)
        before_frame = self._capture("before_command")
        key = (command.arm, command.joint)
        total = self._travel.get(key, 0.0) + command.delta_deg
        if abs(total) > self.config.max_total_deg_per_joint + 1e-9:
            raise SafetyAbort(
                "TRAVEL_LIMIT",
                "arm %d joint %d cumulative %.2f exceeds %.2f"
                % (command.arm, command.joint, abs(total),
                   self.config.max_total_deg_per_joint),
            )

        try:
            response = self.transport.arm(command)
        except Exception as exc:
            if not self.transport.lease_valid():
                raise SafetyAbort("LEASE_LOST", "command refused: %s" % exc)
            raise
        if not isinstance(response, Mapping) or response.get("ok") is not True:
            detail = response.get("error") if isinstance(response, Mapping) else response
            raise SafetyAbort("COMMAND_REJECTED",
                              "arm %d joint %d: %s"
                              % (command.arm, command.joint, detail))
        targets = response.get("targets_deg")
        if not isinstance(targets, list) or len(targets) != 6:
            raise SafetyAbort("COMMAND_REJECTED", "response has no six targets")
        numeric = [_number(v) for v in targets]
        if any(v is None for v in numeric):
            raise SafetyAbort("COMMAND_REJECTED", "response targets are not finite")
        expected = before_snap.targets[command.joint] + command.delta_deg
        actual = float(numeric[command.joint])  # type: ignore[arg-type]
        if abs(actual - expected) > 0.05:
            raise SafetyAbort(
                "TARGET_CLAMPED",
                "arm %d joint %d wanted %.3f, server held %.3f"
                % (command.arm, command.joint, expected, actual),
            )
        self._commands += 1
        self._travel[key] = total
        # Record that the command left the process before waiting for motion.
        # If the arm stalls or feedback goes stale, the audit still shows what
        # was sent and the state/image it was based on.
        self.log.emit(
            "command_sent",
            kind="arm",
            command=command.as_dict(),
            expected_target_deg=round(expected, 3),
            server_target_deg=round(actual, 3),
            before_arm=before_snap.summary(),
            before_image=before_frame.ref(),
        )

        self._wait_settle(command.arm, command.joint, actual)
        after_status = self._check_safety("after_command", [command.arm])
        after_snap = arm_snapshot(after_status, command.arm)
        after_frame = self._capture("after_command")
        self.log.emit(
            "action",
            kind="arm",
            command=command.as_dict(),
            expected_target_deg=round(expected, 3),
            server_target_deg=round(actual, 3),
            before_arm=before_snap.summary(),
            after_arm=after_snap.summary(),
            before_image=before_frame.ref(),
            after_image=after_frame.ref(),
            result="ok",
        )

    def _wait_settle(self, arm: int, joint: int, target: float) -> None:
        deadline = self.clock() + self.config.settle_timeout_s
        while True:
            status = self._check_safety("settle", [arm])
            snap = arm_snapshot(status, arm)
            if abs(snap.joints[joint] - target) <= self.config.settle_tolerance_deg:
                return
            if self.clock() >= deadline:
                raise SafetyAbort(
                    "STALLED",
                    "arm %d joint %d measured %.2f did not reach %.2f"
                    % (arm, joint, snap.joints[joint], target),
                )
            self.sleep(self.config.settle_poll_s)

    def _close_gripper(self, arm: int) -> Tuple[bool, str]:
        snap = arm_snapshot(self._check_safety("close_start", [arm]), arm)
        if snap.gripper_deg <= 0.5:
            return False, "already_closed"
        step = -abs(self.config.gripper_step_deg)
        for _ in range(self.config.gripper_max_segments):
            if self._commands >= self.config.max_commands:
                raise SafetyAbort("COMMAND_LIMIT",
                                  "run exceeded %d commands" % self.config.max_commands)
            before_status = self._check_safety("before_gripper", [arm])
            before_snap = arm_snapshot(before_status, arm)
            before_frame = self._capture("before_gripper")
            if before_snap.gripper_deg <= 0.5:
                return False, "closed_no_contact"
            try:
                response = self.transport.gripper(GripperCommand(arm, step))
            except Exception as exc:
                if not self.transport.lease_valid():
                    raise SafetyAbort("LEASE_LOST", "gripper refused: %s" % exc)
                raise
            if not isinstance(response, Mapping) or response.get("ok") is not True:
                detail = response.get("error") if isinstance(response, Mapping) else response
                raise SafetyAbort("GRIPPER_REJECTED", str(detail))
            if _number(response.get("gripper_target_deg")) is None:
                raise SafetyAbort("GRIPPER_REJECTED",
                                  "response has no finite target")
            self._commands += 1
            self.log.emit(
                "command_sent",
                kind="gripper",
                command=GripperCommand(arm, step).body(),
                before_arm=before_snap.summary(),
                before_image=before_frame.ref(),
            )
            self.sleep(self.config.gripper_settle_s)
            after_status = self._check_safety("after_gripper", [arm])
            after_snap = arm_snapshot(after_status, arm)
            after_frame = self._capture("after_gripper")
            gap = abs(after_snap.gripper_target_deg - after_snap.gripper_deg)
            stable = abs(after_snap.gripper_deg - before_snap.gripper_deg) <= 0.3
            contact = gap >= self.config.contact_min_lag_deg
            if contact and self.config.contact_samples > 1:
                contact = self._confirm_contact(arm, after_snap.gripper_deg)
            self.log.emit(
                "action",
                kind="gripper",
                command=GripperCommand(arm, step).body(),
                gap_deg=round(gap, 3),
                stable=stable,
                contact_candidate=bool(contact),
                before_arm=before_snap.summary(),
                after_arm=after_snap.summary(),
                before_image=before_frame.ref(),
                after_image=after_frame.ref(),
                result="ok",
            )
            if contact:
                return True, "contact_candidate_gap_%.2f" % gap
            if after_snap.gripper_deg <= 0.5 and after_snap.gripper_target_deg <= 0.5:
                return False, "closed_no_contact"
        return False, "max_segments"

    def _confirm_contact(self, arm: int, measured: float) -> bool:
        for _ in range(max(0, self.config.contact_samples - 1)):
            self.sleep(self.config.gripper_settle_s)
            snap = arm_snapshot(self._check_safety("contact_confirm", [arm]), arm)
            gap = abs(snap.gripper_target_deg - snap.gripper_deg)
            if gap < self.config.contact_min_lag_deg or \
                    abs(snap.gripper_deg - measured) > 0.5:
                return False
        return True

    def _observe_board(self, label: str) -> Tuple[Optional[Frame], Optional[BoardObservation]]:
        frame = self._capture(label)
        if self.vision_probe is None or self.dry_run:
            # A dry run is offline: do not import or call a detector either.
            return frame, None
        try:
            raw = self.vision_probe(frame)
        except SafetyAbort:
            raise
        except Exception as exc:
            raise SafetyAbort("VISION_FAILED", str(exc))
        if raw is None:
            raise SafetyAbort("VISION_FAILED", "probe returned no observation")
        obs = BoardObservation.from_value(raw, frame.fetched_at)
        self.log.emit("board", label=label, observation=obs.as_dict(),
                      image=frame.ref())
        return frame, obs

    def _cleanup(self) -> None:
        errors = []
        actions = (
            ("safe_stop", self.transport.safe_stop, ()),
            ("stop_recording", self.transport.stop_recording,
             (self._verified_success,)),
            ("release", self.transport.release, ()),
        )
        for name, fn, args in actions:
            try:
                outcome = fn(*args)
            except Exception as exc:
                errors.append("%s: %s" % (name, exc))
                continue
            if outcome is False:
                errors.append("%s returned failure" % name)
        self.log.emit("cleanup", ok=not errors, errors=errors)


def _plan_arms(*plans: CommandPlan) -> Tuple[int, ...]:
    arms = set()
    for plan in plans:
        for command in plan.commands:
            arms.add(command.arm)
    return tuple(sorted(arms))


# ---------------------------------------------------------------------------
# helpers for CLI / plan documents
# ---------------------------------------------------------------------------


def parse_joint_ref(value: Any) -> int:
    """Accept 0..5 or "J1".."J6" (1-based, the human convention)."""
    if isinstance(value, str):
        text = value.strip().upper()
        if text.startswith("J") and text[1:].isdigit():
            return validate_joint(int(text[1:]) - 1)
        raise ValidationError("joint name must be J1..J6, got %r" % value)
    return validate_joint(value)


def parse_extend_spec(spec: str, arm: Any = 0) -> List[JointGoal]:
    """Parse ``J1:+20,J2:+10,J3:-5,J4:+8`` into ordered goals."""
    arm_i = validate_arm(arm)
    goals: List[JointGoal] = []
    if not spec:
        return goals
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        for sep in ("=", ":"):
            if sep in chunk:
                name, text = chunk.split(sep, 1)
                break
        else:
            raise ValidationError("bad extend item %r; use J1:+20" % chunk)
        joint = parse_joint_ref(name)
        try:
            delta = float(text)
        except (TypeError, ValueError):
            raise ValidationError("bad delta %r for %s" % (text, name)) from None
        goals.append(JointGoal.delta(arm_i, joint,
                                     _finite(delta, "delta_deg")))
    return goals


def load_vision_probe(spec: str) -> Callable[[Frame], Any]:
    """Load ``module:callable`` or ``path/to/file.py:callable``."""
    if ":" not in spec:
        raise ValidationError("--vision must be module:function")
    where, attr = spec.rsplit(":", 1)
    if not where or not attr:
        raise ValidationError("--vision must be module:function")
    if where.endswith(".py") or os.path.sep in where or "/" in where:
        path = os.path.abspath(where)
        mod_name = "_rule_grasp_vision_%d" % abs(hash(path))
        spec_obj = importlib.util.spec_from_file_location(mod_name, path)
        if spec_obj is None or spec_obj.loader is None:
            raise ValidationError("cannot load vision module %s" % path)
        module = importlib.util.module_from_spec(spec_obj)
        spec_obj.loader.exec_module(module)
    else:
        import importlib
        module = importlib.import_module(where)
    fn = getattr(module, attr, None)
    if not callable(fn):
        raise ValidationError("vision attribute %r is not callable" % attr)
    return fn


def demo_plan_doc(arm: Any = 0,
                  spec: str = "J1:+20,J2:+10,J3:-8,J4:+8") -> Dict[str, Any]:
    """A small, in-limit extension sequence for a dry run."""
    goals = parse_extend_spec(spec, arm)
    return {
        "arm": validate_arm(arm),
        "align": [],
        "approach": [{"joint": g.joint, "delta_deg": g.delta_deg}
                     for g in goals],
        "gripper": {"arm": validate_arm(arm), "close_step_deg": 5.0,
                    "max_segments": 12},
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _default_log_path(run_id: str) -> str:
    root = os.path.expanduser(os.environ.get(
        "AGILEX_RULE_GRASP_LOG_DIR", "~/agilex-data/rule-grasp"))
    return os.path.join(root, "%s.jsonl" % run_id)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Rule-based calibration-board grasp.  Dry run by default; "
                    "real HTTP needs --execute --confirm EXECUTE --people-clear.")
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--plan", help="JSON plan with align/approach move lists")
    src.add_argument("--extend", help="J1:+20,J2:+10,J3:-8,J4:+8")
    src.add_argument("--demo", action="store_true", help="built-in dry-run plan")

    ap.add_argument("--execute", action="store_true",
                    help="send real HTTP; requires --confirm EXECUTE, "
                         "--people-clear and --vision")
    ap.add_argument("--confirm", default=None,
                    help="must be exactly EXECUTE with --execute")
    ap.add_argument("--people-clear", action="store_true",
                    help="operator confirms the area around the robot is clear")
    ap.add_argument("--vision", default=None,
                    help="module:function returning a BoardObservation dict")
    ap.add_argument("--api", default=os.environ.get("AGILEX_API",
                                                    "http://127.0.0.1:8765"))
    ap.add_argument("--controller", default=None,
                    help="controller id for the lease audit trail")
    ap.add_argument("--arm", type=int, default=0, choices=(0, 1))
    ap.add_argument("--cam", type=int, default=4,
                    help="camera for board frames (0,2 wrist; 4,12 global)")
    ap.add_argument("--log", default=None,
                    help="JSONL path; default ~/agilex-data/rule-grasp/<run>.jsonl")
    ap.add_argument("--start-pose", default=None,
                    help="dry-run only: six comma-separated degrees")
    ap.add_argument("--max-step", type=float, default=MAX_API_STEP_DEG)
    ap.add_argument("--max-total", type=float, default=45.0,
                    help="max cumulative travel per joint, degrees")
    ap.add_argument("--max-lag", type=float, default=6.0)
    ap.add_argument("--max-start-lag", type=float, default=1.0,
                    help="target-vs-measured lag allowed before planning, deg")
    ap.add_argument("--max-feedback-age", type=float, default=1.5)
    ap.add_argument("--max-image-age", type=float, default=2.0)
    ap.add_argument("--max-duration", type=float, default=180.0)
    ap.add_argument("--people-clear-ttl", type=float, default=900.0)
    ap.add_argument("--heartbeat", type=float, default=5.0)
    ap.add_argument("--settle-timeout", type=float, default=4.0)
    ap.add_argument("--start-recording", action="store_true",
                    help="send /api/record/start before motion (execute only)")
    ap.add_argument("--task", default="calibration board grasp",
                    help="recording task label")
    return ap


def _load_plan_doc(args: argparse.Namespace) -> Dict[str, Any]:
    if args.plan:
        with open(args.plan, "r", encoding="utf-8") as handle:
            doc = json.load(handle)
        if not isinstance(doc, dict):
            raise ValidationError("plan file must contain a JSON object")
        return doc
    if args.extend:
        return demo_plan_doc(args.arm, args.extend)
    return demo_plan_doc(args.arm)


def _parse_start_pose(text: Optional[str]) -> Tuple[float, ...]:
    if not text:
        return (0.0,) * 6
    parts = [p.strip() for p in text.split(",") if p.strip()]
    if len(parts) != 6:
        raise ValidationError("--start-pose needs six comma-separated degrees")
    try:
        values = [float(p) for p in parts]
    except ValueError:
        raise ValidationError("--start-pose values must be numbers") from None
    return _validate_pose(values)


def _gripper_config(doc: Mapping[str, Any], base: RuleConfig) -> RuleConfig:
    """Apply optional plan-level gripper settings, with strict types."""
    raw = doc.get("gripper")
    if raw is None:
        return base
    if not isinstance(raw, Mapping):
        raise ValidationError("gripper must be an object")
    step = raw.get("close_step_deg", base.gripper_step_deg)
    segments = raw.get("max_segments", base.gripper_max_segments)
    return RuleConfig(
        max_step_deg=base.max_step_deg,
        max_total_deg_per_joint=base.max_total_deg_per_joint,
        max_commands=base.max_commands,
        max_plan_duration_s=base.max_plan_duration_s,
        max_lag_deg=base.max_lag_deg,
        max_start_lag_deg=base.max_start_lag_deg,
        max_feedback_age_s=base.max_feedback_age_s,
        max_image_age_s=base.max_image_age_s,
        settle_timeout_s=base.settle_timeout_s,
        settle_poll_s=base.settle_poll_s,
        settle_tolerance_deg=base.settle_tolerance_deg,
        heartbeat_interval_s=base.heartbeat_interval_s,
        people_clear_ttl_s=base.people_clear_ttl_s,
        require_enabled_all=base.require_enabled_all,
        gripper_step_deg=_finite(step, "close_step_deg"),
        gripper_max_segments=validate_positive_int(segments, "max_segments"),
        gripper_settle_s=base.gripper_settle_s,
        contact_min_lag_deg=base.contact_min_lag_deg,
        contact_samples=base.contact_samples,
        verify=base.verify,
    )


def validate_positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValidationError("%s must be a positive integer, got %r" % (name, value))
    return value


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        validate_arm(args.arm)
    except ValidationError as exc:
        print("refused: %s" % exc, file=sys.stderr)
        return 2

    if args.execute:
        if args.confirm != "EXECUTE":
            print("refused: --execute requires --confirm EXECUTE", file=sys.stderr)
            return 2
        if not args.people_clear:
            print("refused: --execute requires --people-clear", file=sys.stderr)
            return 2
        if not args.vision:
            print("refused: --execute requires --vision module:function; a "
                  "success needs fresh board evidence", file=sys.stderr)
            return 2
        if args.start_pose:
            print("refused: --start-pose is a dry-run option", file=sys.stderr)
            return 2

    try:
        doc = _load_plan_doc(args)
        vision = load_vision_probe(args.vision) if args.vision else None
        config = RuleConfig(
            max_step_deg=args.max_step,
            max_total_deg_per_joint=args.max_total,
            max_lag_deg=args.max_lag,
            max_start_lag_deg=args.max_start_lag,
            max_feedback_age_s=args.max_feedback_age,
            max_image_age_s=args.max_image_age,
            max_plan_duration_s=args.max_duration,
            heartbeat_interval_s=args.heartbeat,
            people_clear_ttl_s=args.people_clear_ttl,
            settle_timeout_s=args.settle_timeout,
            verify=VerificationConfig(),
        )
        config = _gripper_config(doc, config)
    except (ValueError, OSError, json.JSONDecodeError, ImportError,
            AttributeError) as exc:
        print("refused: %s" % exc, file=sys.stderr)
        return 2

    run_id = "rule-grasp-%d" % int(time.time())
    log_path = args.log or _default_log_path(run_id)
    try:
        log = EventLog(log_path, run_id)
    except OSError as exc:
        print("refused: cannot open the event log: %s" % exc, file=sys.stderr)
        return 2

    controller_id = args.controller or "rule-grasp-%s-%d" % (
        os.uname().nodename if hasattr(os, "uname") else "client", os.getpid())

    if args.execute:
        transport: Transport = HttpTransport(args.api, controller_id)
        try:
            acquired = transport.acquire()
        except Exception as exc:
            print("lease acquire failed: %s" % exc, file=sys.stderr)
            return 1
        if not acquired:
            print("refused: server has no lease API; execute mode is fail-closed",
                  file=sys.stderr)
            return 1
        print("LIVE | lease acquired | controller=%s | api=%s"
              % (controller_id, args.api))
        print("LIVE | people-clear confirmed | max_step=%.2f deg | cam=%d"
              % (config.max_step_deg, args.cam))
    else:
        start = _parse_start_pose(args.start_pose)
        transport = SimulatedTransport(start_joints=(start, start),
                                       clock=time.time)
        print("DRY RUN: no network, no lease, no robot; success is not claimed")

    controller = RuleGraspController(
        transport, config=config, log=log,
        people_clear=True if not args.execute else args.people_clear,
        vision_probe=vision, vision_cam=args.cam, dry_run=not args.execute,
        run_id=run_id,
    )

    try:
        align_plan, approach_plan = controller.plan(doc)
    except (ValidationError, SafetyAbort, RuleGraspError) as exc:
        print("plan refused: %s" % exc, file=sys.stderr)
        if args.execute:
            transport.safe_stop()
            transport.release()
        return 2

    print("plan: align=%d commands, approach=%d commands"
          % (align_plan.command_count, approach_plan.command_count))
    for command in align_plan.commands + approach_plan.commands:
        print("  arm=%d joint=%d delta=%+.3f"
              % (command.arm, command.joint, command.delta_deg))

    if args.execute and args.start_recording:
        try:
            transport.lease.post("/api/record/start",
                                 {"task": args.task, "notes": "rule-grasp"})
        except Exception as exc:
            print("record start failed: %s" % exc, file=sys.stderr)
            transport.safe_stop()
            transport.release()
            return 1

    gripper_doc = doc.get("gripper") if isinstance(doc, dict) else None
    gripper_arm = args.arm
    if isinstance(gripper_doc, Mapping) and gripper_doc.get("arm") is not None:
        gripper_arm = validate_arm(gripper_doc.get("arm"))

    result = controller.run(align_plan, approach_plan, gripper_arm=gripper_arm)
    print(json.dumps(result.as_dict(), sort_keys=True, ensure_ascii=False))
    print("log: %s" % log_path)
    if result.success:
        return 0
    if result.dry_run and result.reason == "DRY_RUN_NOT_VERIFIED":
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
