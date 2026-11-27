#!/usr/bin/env python3
"""Offline tests for tools/feedback_control.py.

No CAN, no robot, no network.  The frame bytes below are copied from the
2026-11-24 passive audit (AgileX-实验记录/logs), so the parser is checked
against what the Tracer and Piper actually put on the wire.

    python tools/test-feedback-control.py

The suite is stdlib-only and intentionally independent of the server tests.
"""
import importlib.util
import math
import os
import sys

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
MODULE = os.path.join(HERE, "feedback_control.py")

spec = importlib.util.spec_from_file_location("feedback_control", MODULE)
fc = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = fc
spec.loader.exec_module(fc)

FAIL = []

PULSE_MAP = fc.PulseMapping(left_can_id=0x251, right_can_id=0x252)


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (("  " + str(detail)) if detail and not cond else ""))
    if not cond:
        FAIL.append(name)


def default_iface(can_id):
    return "can0" if can_id in (0x311, 0x251, 0x252) else "can_left"


def parse(can_id, text, timestamp=None, iface=None, clock="monotonic"):
    if iface is None:
        iface = default_iface(can_id)
    return fc.decode_frame(
        can_id,
        bytes.fromhex(text.replace(" ", "")),
        timestamp,
        iface=iface,
        clock=clock,
    )


def raises(exc, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except exc:
        return True
    except Exception:
        return False
    return False


def approx(a, b, eps=1e-9):
    return a is not None and abs(a - b) <= eps


# ---------------------------------------------------------------------------
# 1. Known real frames
# ---------------------------------------------------------------------------
def test_known_frames():
    print("== known real frames ==")
    odom = parse(fc.CAN_ID_ODOMETRY, "0000022800000296")
    check("0x311 left wheel int32 = 552", odom.left == 552, odom.left)
    check("0x311 right wheel int32 = 662", odom.right == 662, odom.right)
    check("0x311 keeps its CAN id", odom.can_id == 0x311, odom.can_id)
    check("0x311 keeps its bus", odom.iface == "can0", odom.iface)
    check("0x311 keeps its clock", odom.clock == "monotonic", odom.clock)

    a1 = parse(fc.CAN_ID_ACTUATOR1_HS, "0000000CFFFCE129")
    check("0x251 rpm = 0", a1.rpm == 0, a1.rpm)
    check("0x251 current = 1.2 A", approx(a1.current_a, 1.2), a1.current_a)
    check("0x251 pulse_count = -204503", a1.pulse_count == -204503, a1.pulse_count)
    check("0x251 keeps its CAN id", a1.can_id == 0x251, a1.can_id)
    check("0x251 keeps can0", a1.iface == "can0", a1.iface)

    a2 = parse(fc.CAN_ID_ACTUATOR2_HS, "00000004 00029B20")
    check("0x252 rpm = 0", a2.rpm == 0, a2.rpm)
    check("0x252 current = 0.4 A", approx(a2.current_a, 0.4), a2.current_a)
    check("0x252 pulse_count = 170784", a2.pulse_count == 170784, a2.pulse_count)
    check("0x252 keeps its CAN id", a2.can_id == 0x252, a2.can_id)

    neg = parse(fc.CAN_ID_ACTUATOR1_HS, "FFFEFFF4FFFFFFFF")
    check("0x251 negative rpm is signed", neg.rpm == -2, neg.rpm)
    check("0x251 negative current is signed", approx(neg.current_a, -1.2), neg.current_a)
    check("0x251 negative pulse is signed", neg.pulse_count == -1, neg.pulse_count)

    j12 = parse(fc.CAN_ID_ARM_JOINT_12, "00004EEC00000000")
    check("0x2A5 J1 = 20.204 deg", approx(j12.joints_deg[0], 20.204), j12.joints_deg)
    check("0x2A5 J2 = 0.000 deg", approx(j12.joints_deg[1], 0.0), j12.joints_deg)
    check("0x2A5 starts at joint 1", j12.first_joint == 1, j12.first_joint)

    j34 = parse(fc.CAN_ID_ARM_JOINT_34, "FFFFEC22FFFF0E78")
    check("0x2A6 J3 = -5.086 deg", approx(j34.joints_deg[0], -5.086), j34.joints_deg)
    check("0x2A6 J4 = -61.832 deg", approx(j34.joints_deg[1], -61.832), j34.joints_deg)
    check("0x2A6 starts at joint 3", j34.first_joint == 3, j34.first_joint)

    j56 = parse(fc.CAN_ID_ARM_JOINT_56, "00006F3700009E9C")
    check("0x2A7 J5 = 28.471 deg", approx(j56.joints_deg[0], 28.471), j56.joints_deg)
    check("0x2A7 J6 = 40.604 deg", approx(j56.joints_deg[1], 40.604), j56.joints_deg)
    check("0x2A7 starts at joint 5", j56.first_joint == 5, j56.first_joint)

    st = parse(fc.CAN_ID_ARM_STATUS, "0100010200000000")
    check("0x2A1 ctrl_mode = 1 (CAN)", st.ctrl_mode == 1, st.ctrl_mode)
    check("0x2A1 arm_status = 0 (normal)", st.arm_status == 0, st.arm_status)
    check("0x2A1 mode_feed = 1 (MOVE_J)", st.mode_feed == 1, st.mode_feed)
    check("0x2A1 teach_status = 2 (stop recording)", st.teach_status == 2, st.teach_status)
    check("0x2A1 motion_status = 0 (at target)", st.motion_status == 0, st.motion_status)
    check("0x2A1 trajectory_num = 0", st.trajectory_num == 0, st.trajectory_num)
    check("0x2A1 err_code = 0", st.error_code == 0, st.error_code)
    check("0x2A1 mode name is MOVE_J", st.mode_name == "MOVE_J", st.mode_name)
    check("0x2A1 normal frame has no fault", st.fault is None, st.fault)

    ec = parse(fc.CAN_ID_ARM_STATUS, "0100010200000A20")
    check("0x2A1 err_code is big-endian 0x0A20", ec.error_code == 0x0A20, hex(ec.error_code))
    check("0x2A1 err_code maps to a fault", ec.fault is not None, ec.fault)

    teach = parse(fc.CAN_ID_ARM_STATUS, "0100010B00000000")
    check("0x2A1 teaching status 0x0B is not a fault", teach.fault is None, teach.fault)

    moving = parse(fc.CAN_ID_ARM_STATUS, "0100010201000000")
    check("0x2A1 motion_status=1 (not reached) is not a fault", moving.fault is None, moving.fault)

    mode = parse(fc.CAN_ID_ARM_STATUS, "0100FF0000000000")
    check("0x2A1 unknown mode_feed -> UNKNOWN", mode.mode_name == "UNKNOWN", mode.mode_name)


# ---------------------------------------------------------------------------
# 2. candump lines
# ---------------------------------------------------------------------------
def test_candump_lines():
    print("== candump lines ==")
    line = "(1795519709.546247) can_left 2A7#00006F3700009E9C"
    frame = fc.parse_candump_line(line)
    check("candump timestamp parsed", approx(frame.timestamp, 1795519709.546247, 1e-6), frame.timestamp)
    check("candump can_id parsed", frame.can_id == 0x2A7, hex(frame.can_id))
    check("candump iface preserved", frame.iface == "can_left", frame.iface)
    check("candump clock is epoch", frame.clock == "epoch", frame.clock)
    check("candump payload length 8", len(frame.data) == 8, len(frame.data))
    decoded = fc.decode_frame(frame.can_id, frame.data, frame.timestamp,
                              iface=frame.iface, clock=frame.clock)
    check("candump frame decodes to joint 5", approx(decoded.joints_deg[0], 28.471), decoded.joints_deg)
    check("candump decoded frame keeps epoch", decoded.clock == "epoch", decoded.clock)

    bare = fc.parse_candump_line("can0 311#0000022800000296")
    check("candump without timestamp accepted", bare.timestamp is None, bare.timestamp)
    check("candump without timestamp has no clock", bare.clock is None, bare.clock)
    check("candump without timestamp decodes",
          fc.decode_frame(bare.can_id, bare.data, iface=bare.iface).left == 552)

    for bad in (
        "",
        "garbage",
        "311#0000",
        "311#000002280000029G",
        "311#000002280000029600",
        "(123.0) can0",
        "(abc) can0 311#0000022800000296",
        "can0 3110000022800000296",
    ):
        check("bad candump line rejected: %r" % bad,
              raises(fc.FrameError, fc.parse_candump_line, bad))


# ---------------------------------------------------------------------------
# 3. Invalid / unsupported frames
# ---------------------------------------------------------------------------
def test_invalid_frames():
    print("== invalid frames ==")
    check("0x311 short payload rejected",
          raises(fc.FrameError, fc.decode_frame, 0x311, b"\x00" * 7, iface="can0"))
    check("0x311 long payload rejected",
          raises(fc.FrameError, fc.decode_frame, 0x311, b"\x00" * 9, iface="can0"))
    check("0x251 short payload rejected",
          raises(fc.FrameError, fc.decode_frame, 0x251, b"\x00" * 6, iface="can0"))
    check("0x2A5 short payload rejected",
          raises(fc.FrameError, fc.decode_frame, 0x2A5, b"\x00" * 4, iface="can_left"))
    check("0x2A1 short payload rejected",
          raises(fc.FrameError, fc.decode_frame, 0x2A1, b"\x00" * 7, iface="can_left"))
    check("0x2A1 long payload rejected",
          raises(fc.FrameError, fc.decode_frame, 0x2A1, b"\x00" * 9, iface="can_left"))

    for can_id in (0x313, 0x221, 0x211, 0x2A8, 0x111):
        check("unsupported CAN id 0x%03X rejected" % can_id,
              raises(fc.UnsupportedFrame, fc.decode_frame, can_id, b"\x00" * 8))

    check("UnsupportedFrame is a FrameError",
          issubclass(fc.UnsupportedFrame, fc.FrameError))
    check("FrameBusError is a FrameError",
          issubclass(fc.FrameBusError, fc.FrameError))
    check("0x311 payload is bytes-like",
          raises(TypeError, fc.decode_frame, 0x311, "0000022800000296", iface="can0"))

    check("0x311 on an arm bus rejected",
          raises(fc.FrameBusError, fc.decode_frame, 0x311, b"\x00" * 8, iface="can_left"))
    check("0x251 on an arm bus rejected",
          raises(fc.FrameBusError, fc.decode_frame, 0x251, b"\x00" * 8, iface="can_left"))
    check("0x252 on an arm bus rejected",
          raises(fc.FrameBusError, fc.decode_frame, 0x252, b"\x00" * 8, iface="can_right"))
    check("0x2A1 on the chassis bus rejected",
          raises(fc.FrameBusError, fc.decode_frame, 0x2A1, b"\x00" * 8, iface="can0"))
    check("0x2A5 on the chassis bus rejected",
          raises(fc.FrameBusError, fc.decode_frame, 0x2A5, b"\x00" * 8, iface="can0"))
    check("0x2A7 on can_right accepted",
          fc.decode_frame(0x2A7, bytes.fromhex("00006F3700009E9C"),
                          iface="can_right").iface == "can_right")
    check("bus is required for 0x311",
          raises(fc.FrameBusError, fc.decode_frame, 0x311, b"\x00" * 8))
    check("bus is required for 0x2A5",
          raises(fc.FrameBusError, fc.decode_frame, 0x2A5, b"\x00" * 8))


# ---------------------------------------------------------------------------
# 4. Calibration and distance
# ---------------------------------------------------------------------------
def counts(ts, left, right, source="wheel_0x311", mapping=None,
           iface="can0", clock="monotonic"):
    return fc.WheelCounts(
        timestamp=ts, left=left, right=right, source=source,
        iface=iface, clock=clock, pulse_mapping=mapping,
    )


def test_calibration():
    print("== calibration and distance ==")
    cal = fc.WheelCalibration(counts_per_meter=1000.0, track_width_m=0.4)
    check("calibration keeps counts_per_meter", cal.counts_per_meter == 1000.0)
    check("calibration keeps track_width", cal.track_width_m == 0.4)
    check("calibration declares its source", cal.source == "wheel_0x311", cal.source)
    check("calibration defaults both wheel signs to +1",
          cal.left_sign == 1 and cal.right_sign == 1)

    mirrored = fc.WheelCalibration(
        counts_per_meter=1000.0, track_width_m=0.4,
        source="actuator_pulse", left_sign=-1, pulse_mapping=PULSE_MAP,
    )
    mirror_motion = fc.wheel_motion(
        fc.WheelCounts(10.0, 0, 0, source="actuator_pulse",
                       iface="can0", clock="monotonic", pulse_mapping=PULSE_MAP),
        fc.WheelCounts(10.1, -100, 100, source="actuator_pulse",
                       iface="can0", clock="monotonic", pulse_mapping=PULSE_MAP),
        mirrored,
    )
    check("mirrored left pulse counts forward as positive",
          approx(mirror_motion.distance_m, 0.1), mirror_motion.distance_m)
    check("mirrored pulse pair has zero yaw", approx(mirror_motion.yaw_rad, 0.0),
          mirror_motion.yaw_rad)

    prev = counts(10.0, 0, 0)
    fwd = fc.wheel_motion(prev, counts(10.1, 100, 100), cal)
    check("forward motion is valid", fwd.valid is True, fwd)
    check("100 counts at 1000/m = 0.1 m", approx(fwd.distance_m, 0.1), fwd.distance_m)
    check("straight motion has zero yaw", approx(fwd.yaw_rad, 0.0), fwd.yaw_rad)
    check("dt is 0.1 s", approx(fwd.dt_s, 0.1), fwd.dt_s)

    rev = fc.wheel_motion(counts(10.0, 100, 100), counts(10.1, 0, 0), cal)
    check("reverse motion is negative", approx(rev.distance_m, -0.1), rev.distance_m)
    check("reverse yaw is zero", approx(rev.yaw_rad, 0.0), rev.yaw_rad)

    right = fc.wheel_motion(prev, counts(10.1, 80, 120), cal)
    check("right wheel faster -> positive yaw", approx(right.yaw_rad, 0.1), right.yaw_rad)
    check("differential keeps mean distance", approx(right.distance_m, 0.1), right.distance_m)

    left = fc.wheel_motion(prev, counts(10.1, 120, 80), cal)
    check("left wheel faster -> negative yaw", approx(left.yaw_rad, -0.1), left.yaw_rad)

    cal2 = fc.WheelCalibration(counts_per_meter=2000.0, track_width_m=0.4)
    half = fc.wheel_motion(prev, counts(10.1, 100, 100), cal2)
    check("counts_per_meter changes the scale", approx(half.distance_m, 0.05), half.distance_m)

    narrow = fc.WheelCalibration(counts_per_meter=1000.0, track_width_m=0.2)
    sharper = fc.wheel_motion(prev, counts(10.1, 80, 120), narrow)
    check("track_width changes the yaw scale", approx(sharper.yaw_rad, 0.2), sharper.yaw_rad)

    no_cal = fc.wheel_motion(prev, counts(10.1, 100, 100), None)
    check("missing calibration is refused", no_cal.valid is False, no_cal)
    check("missing calibration code is UNCALIBRATED", no_cal.code == "UNCALIBRATED", no_cal.code)
    check("missing calibration returns no metres", no_cal.distance_m is None, no_cal.distance_m)
    check("missing calibration returns no yaw", no_cal.yaw_rad is None, no_cal.yaw_rad)

    mismatch = fc.wheel_motion(
        counts(10.0, 0, 0, source="actuator_pulse"),
        counts(10.1, 100, 100, source="actuator_pulse"),
        cal,
    )
    check("calibration source mismatch is refused", mismatch.valid is False, mismatch)
    check("source mismatch code is UNCALIBRATED", mismatch.code == "UNCALIBRATED", mismatch.code)

    same_ts = fc.wheel_motion(prev, counts(10.0, 100, 100), cal)
    check("zero dt is refused", same_ts.valid is False, same_ts)
    check("zero dt code is STALE", same_ts.code == "STALE", same_ts.code)
    check("zero dt returns no metres", same_ts.distance_m is None, same_ts.distance_m)

    backwards_ts = fc.wheel_motion(counts(10.1, 0, 0), counts(10.0, 100, 100), cal)
    check("negative dt is refused", backwards_ts.valid is False, backwards_ts)

    long_dt = fc.wheel_motion(counts(10.0, 0, 0), counts(20.0, 100, 100), cal,
                              max_dt_s=0.5)
    check("dt above max_dt is refused", long_dt.valid is False, long_dt)
    check("dt above max_dt code is STALE", long_dt.code == "STALE", long_dt.code)
    check("dt above max_dt returns no metres", long_dt.distance_m is None,
          long_dt.distance_m)

    epoch_motion = fc.wheel_motion(
        counts(10.0, 0, 0, clock="epoch"),
        counts(10.1, 100, 100, clock="monotonic"),
        cal,
    )
    check("mixed clock domains refused", epoch_motion.valid is False, epoch_motion)
    check("mixed clock code is CLOCK_MISMATCH",
          epoch_motion.code == "CLOCK_MISMATCH", epoch_motion.code)
    unknown_clock = fc.wheel_motion(
        counts(10.0, 0, 0, clock=None),
        counts(10.1, 100, 100, clock=None),
        cal,
    )
    check("unknown clock refused", unknown_clock.valid is False, unknown_clock)
    check("unknown clock code is CLOCK_UNKNOWN",
          unknown_clock.code == "CLOCK_UNKNOWN", unknown_clock.code)

    check("int32 delta handles forward wrap",
          fc.int32_delta(-(2 ** 31) + 4, 2 ** 31 - 5) == 9)
    check("int32 delta handles reverse wrap",
          fc.int32_delta(2 ** 31 - 5, -(2 ** 31) + 4) == -9)
    check("int32 delta keeps a normal delta",
          fc.int32_delta(105, 100) == 5)
    wrapped = fc.wheel_motion(
        counts(10.0, 2 ** 31 - 5, 2 ** 31 - 5),
        counts(10.1, -(2 ** 31) + 4, -(2 ** 31) + 4),
        cal,
    )
    check("wheel_motion crosses int32 wrap", wrapped.valid is True, wrapped)
    check("wrap delta is 9 counts", approx(wrapped.distance_m, 0.009),
          wrapped.distance_m)

    check("wheel_motion requires calibration argument",
          raises(TypeError, fc.wheel_motion, prev, counts(10.1, 100, 100)))

    for bad in (
        dict(counts_per_meter=0.0, track_width_m=0.4),
        dict(counts_per_meter=-1.0, track_width_m=0.4),
        dict(counts_per_meter=1000.0, track_width_m=0.0),
        dict(counts_per_meter=1000.0, track_width_m=-0.4),
        dict(counts_per_meter=1000.0, track_width_m=None),
        dict(counts_per_meter=None, track_width_m=0.4),
        dict(counts_per_meter=1000.0, track_width_m=0.4, left_sign=0),
        dict(counts_per_meter=1000.0, track_width_m=0.4, right_sign=2),
        dict(counts_per_meter=1000.0, track_width_m=0.4, left_sign=True),
        dict(counts_per_meter=1000.0, track_width_m=0.4,
             source="actuator_pulse"),
        dict(counts_per_meter=1000.0, track_width_m=0.4,
             source="wheel_0x311", pulse_mapping=PULSE_MAP),
    ):
        check("invalid calibration rejected: %s" % bad,
              raises(ValueError, fc.WheelCalibration, **bad))

    pulses = fc.pair_actuator_pulse(
        parse(fc.CAN_ID_ACTUATOR1_HS, "0000000C00000064",
              timestamp=10.0, iface="can0", clock="monotonic"),
        parse(fc.CAN_ID_ACTUATOR2_HS, "0000000400000064",
              timestamp=10.0, iface="can0", clock="monotonic"),
        PULSE_MAP,
    )
    check("actuator pair keeps left pulse", pulses.left == 100, pulses.left)
    check("actuator pair keeps right pulse", pulses.right == 100, pulses.right)
    check("actuator pair labels its source", pulses.source == "actuator_pulse", pulses.source)
    check("actuator pair keeps the explicit mapping",
          pulses.pulse_mapping == PULSE_MAP, pulses.pulse_mapping)
    check("actuator pair keeps can0", pulses.iface == "can0", pulses.iface)
    check("actuator pair keeps the clock", pulses.clock == "monotonic", pulses.clock)

    reversed_map = fc.PulseMapping(left_can_id=0x252, right_can_id=0x251)
    reversed_pulses = fc.pair_actuator_pulse(
        parse(fc.CAN_ID_ACTUATOR1_HS, "0000000C00000064", timestamp=10.0, iface="can0"),
        parse(fc.CAN_ID_ACTUATOR2_HS, "00000004000000C8", timestamp=10.0, iface="can0"),
        reversed_map,
    )
    check("reversed pulse mapping swaps left", reversed_pulses.left == 200,
          reversed_pulses.left)
    check("reversed pulse mapping swaps right", reversed_pulses.right == 100,
          reversed_pulses.right)

    check("pulse pairing requires an explicit mapping",
          raises(TypeError, fc.pair_actuator_pulse,
                 parse(fc.CAN_ID_ACTUATOR1_HS, "0000000C00000064", iface="can0"),
                 parse(fc.CAN_ID_ACTUATOR2_HS, "0000000400000064", iface="can0")))
    check("pulse mapping must use 0x251/0x252",
          raises(ValueError, fc.PulseMapping, left_can_id=0x251, right_can_id=0x251))
    check("pulse mapping rejects other IDs",
          raises(ValueError, fc.PulseMapping, left_can_id=0x253, right_can_id=0x252))

    arm_like = fc.ActuatorHighSpeed(
        timestamp=10.0, rpm=0, current_a=0.0, pulse_count=100,
        can_id=0x251, iface="can_left", clock="monotonic")
    check("pulse pairing rejects an arm-bus frame",
          raises(ValueError, fc.pair_actuator_pulse, arm_like, arm_like, PULSE_MAP))

    pulse_cal = fc.WheelCalibration(
        counts_per_meter=10000.0, track_width_m=0.4,
        source="actuator_pulse", pulse_mapping=PULSE_MAP,
    )
    pulse_motion = fc.wheel_motion(
        counts(10.0, 0, 0, source="actuator_pulse", mapping=PULSE_MAP),
        counts(10.1, 1000, 1000, source="actuator_pulse", mapping=PULSE_MAP),
        pulse_cal,
    )
    check("pulse source needs its own calibration", pulse_motion.valid is True, pulse_motion)
    check("pulse delta converts with its own scale", approx(pulse_motion.distance_m, 0.1),
          pulse_motion.distance_m)
    wrong_map_motion = fc.wheel_motion(
        counts(10.0, 0, 0, source="actuator_pulse", mapping=PULSE_MAP),
        counts(10.1, 1000, 1000, source="actuator_pulse", mapping=reversed_map),
        pulse_cal,
    )
    check("pulse motion rejects a different mapping",
          wrong_map_motion.valid is False, wrong_map_motion)
    check("pulse mapping mismatch is UNCALIBRATED",
          wrong_map_motion.code == "UNCALIBRATED", wrong_map_motion.code)


# ---------------------------------------------------------------------------
# 5. Feedback quality gate
# ---------------------------------------------------------------------------
def test_gate():
    print("== feedback quality gate ==")
    cfg = fc.FeedbackGateConfig(
        max_age_s=0.25,
        max_dt_s=0.5,
        max_count_jump=500,
        max_linear_mps=0.2,
        max_angular_rps=0.5,
    )
    cal = fc.WheelCalibration(counts_per_meter=1000.0, track_width_m=0.4)
    gate = fc.ChassisFeedbackGate(cfg, cal)

    check("no sample -> NO_FEEDBACK",
          gate.evaluate(None, None, 10.0, 0.1, 0.0).code == "NO_FEEDBACK")
    check("future sample -> FUTURE",
          gate.evaluate(counts(10.2, 0, 0), None, 10.0, 0.1, 0.0).code == "FUTURE")
    check("old sample -> STALE",
          gate.evaluate(counts(9.0, 0, 0), None, 10.0, 0.1, 0.0).code == "STALE")
    check("non-finite sample timestamp -> STALE",
          gate.evaluate(counts(float("nan"), 0, 0), None, 10.0, 0.1, 0.0).code == "STALE")
    check("non-finite now -> STALE",
          gate.evaluate(counts(10.0, 0, 0), None, float("nan"), 0.1, 0.0).code == "STALE")
    check("non-finite counts -> STALE",
          gate.evaluate(fc.WheelCounts(10.0, float("nan"), 0, iface="can0",
                                      clock="monotonic"),
                        None, 10.0, 0.1, 0.0).code == "STALE")
    check("clock mismatch -> CLOCK_MISMATCH",
          gate.evaluate(counts(10.0, 0, 0, clock="epoch"), None, 10.0, 0.1, 0.0).code
          == "CLOCK_MISMATCH")
    check("unknown clock -> CLOCK_UNKNOWN",
          gate.evaluate(counts(10.0, 0, 0, clock=None), None, 10.0, 0.1, 0.0).code
          == "CLOCK_UNKNOWN")

    first = gate.evaluate(counts(10.0, 0, 0), None, 10.0, 0.1, 0.0)
    check("first sample is accepted as a baseline", first.allowed is True, first)

    good = gate.evaluate(counts(10.1, 10, 10), counts(10.0, 0, 0), 10.1, 0.1, 0.0)
    check("valid moving sample accepted", good.allowed is True, good)
    check("accepted decision code is ok", good.code == "ok", good.code)

    zero = gate.evaluate(counts(10.1, 0, 0), counts(10.0, 0, 0), 10.1, 0.1, 0.0)
    check("zero counts while commanded -> ZERO_BUT_COMMANDED",
          zero.code == "ZERO_BUT_COMMANDED", zero)

    zero_stop = gate.evaluate(counts(10.1, 0, 0), counts(10.0, 0, 0), 10.1, 0.0, 0.0)
    check("zero counts while stopped is allowed", zero_stop.allowed is True, zero_stop)

    zero_turn = gate.evaluate(counts(10.1, 0, 0), counts(10.0, 0, 0), 10.1, 0.0, 0.2)
    check("zero counts while turning -> ZERO_BUT_COMMANDED",
          zero_turn.code == "ZERO_BUT_COMMANDED", zero_turn)

    noisy_gate = fc.ChassisFeedbackGate(
        fc.FeedbackGateConfig(zero_epsilon_counts=2), cal)
    noisy = noisy_gate.evaluate(counts(10.1, 2, 2), counts(10.0, 0, 0), 10.1, 0.1, 0.0)
    check("count noise inside epsilon is still ZERO_BUT_COMMANDED",
          noisy.code == "ZERO_BUT_COMMANDED", noisy)

    jump = gate.evaluate(counts(10.1, 600, 0), counts(10.0, 0, 0), 10.1, 0.1, 0.0)
    check("count jump -> JUMP", jump.code == "JUMP", jump)

    fast = gate.evaluate(counts(10.1, 30, 30), counts(10.0, 0, 0), 10.1, 0.1, 0.0)
    check("speed above cap -> OUT_OF_RANGE", fast.code == "OUT_OF_RANGE", fast)

    turn_fast = gate.evaluate(counts(10.1, -30, 30), counts(10.0, 0, 0), 10.1, 0.0, 0.1)
    check("angular above cap -> OUT_OF_RANGE", turn_fast.code == "OUT_OF_RANGE", turn_fast)

    no_cal = fc.ChassisFeedbackGate(cfg, None)
    uncal = no_cal.evaluate(counts(10.1, 100, 100), counts(10.0, 0, 0), 10.1, 0.1, 0.0)
    check("gate without calibration -> UNCALIBRATED", uncal.code == "UNCALIBRATED", uncal)

    other = fc.ChassisFeedbackGate(
        cfg, fc.WheelCalibration(1000.0, 0.4, source="actuator_pulse",
                                 pulse_mapping=PULSE_MAP))
    wrong_source = other.evaluate(
        counts(10.1, 100, 100, source="wheel_0x311"),
        counts(10.0, 0, 0, source="wheel_0x311"),
        10.1, 0.1, 0.0,
    )
    check("gate rejects a sample from another source",
          wrong_source.code == "UNCALIBRATED", wrong_source)

    edge = gate.evaluate(counts(10.1, 20, 20), counts(10.0, 0, 0), 10.1, 0.1, 0.0)
    check("velocity equal to the cap is accepted", edge.allowed is True, edge)

    long_gap = gate.evaluate(counts(20.0, 40, 40), counts(10.0, 0, 0), 20.0, 0.1, 0.0)
    check("dt above max_dt -> STALE", long_gap.code == "STALE", long_gap)

    wrap_cfg = fc.FeedbackGateConfig(max_dt_s=0.5)
    wrap_gate = fc.ChassisFeedbackGate(wrap_cfg, cal)
    wrapped = wrap_gate.evaluate(
        counts(10.1, -(2 ** 31) + 4, -(2 ** 31) + 4),
        counts(10.0, 2 ** 31 - 5, 2 ** 31 - 5),
        10.1, 0.1, 0.0,
    )
    check("int32 wrap is not a JUMP", wrapped.allowed is True, wrapped)
    check("int32 wrap keeps a small delta", wrapped.code == "ok", wrapped.code)

    stale_cfg = fc.FeedbackGateConfig(max_age_s=0.05)
    stale_gate = fc.ChassisFeedbackGate(stale_cfg, cal)
    check("age just under the cap is accepted",
          stale_gate.evaluate(counts(10.0, 0, 0), None, 10.049, 0.0, 0.0).allowed is True)
    check("age just over the cap is STALE",
          stale_gate.evaluate(counts(10.0, 0, 0), None, 10.051, 0.0, 0.0).code == "STALE")

    for bad_cfg in (
        fc.FeedbackGateConfig(max_age_s=0.0),
        fc.FeedbackGateConfig(max_dt_s=0.0),
        fc.FeedbackGateConfig(max_count_jump=0),
        fc.FeedbackGateConfig(max_linear_mps=0.0),
        fc.FeedbackGateConfig(max_angular_rps=0.0),
        fc.FeedbackGateConfig(clock=""),
    ):
        check("invalid gate config rejected",
              raises(ValueError, fc.FeedbackGateConfig.validate, bad_cfg))


# ---------------------------------------------------------------------------
# 6. Chassis closed loop
# ---------------------------------------------------------------------------
def test_chassis_loop():
    print("== chassis closed loop ==")
    cal = fc.WheelCalibration(counts_per_meter=1000.0, track_width_m=0.4)
    cfg = fc.ChassisControlConfig()
    check("chassis config defaults are sane",
          cfg.max_linear_mps == 0.08 and cfg.max_angular_rps == 0.15)
    check("chassis config defaults to monotonic clock",
          cfg.clock == "monotonic", cfg.clock)
    check("chassis gate default max_dt is set",
          cfg.gate.max_dt_s > 0.0, cfg.gate.max_dt_s)

    ctl = fc.ChassisVelocityController(cfg, cal)
    none = ctl.step(100.0, 0.1, 0.0, None)
    check("no feedback -> zero command", none.linear_mps == 0.0 and none.angular_rps == 0.0, none)
    check("no feedback status is NO_FEEDBACK",
          none.status is fc.ChassisStatus.NO_FEEDBACK, none.status)
    bad_target = ctl.step(100.0, float("nan"), 0.0, counts(100.0, 0, 0))
    check("non-finite target -> zero command",
          bad_target.linear_mps == 0.0 and bad_target.angular_rps == 0.0, bad_target)
    check("non-finite target status is SAFETY",
          bad_target.status is fc.ChassisStatus.SAFETY, bad_target.status)
    bad_counts = ctl.step(
        100.0, 0.1, 0.0,
        fc.WheelCounts(100.0, float("nan"), 0, iface="can0", clock="monotonic"))
    check("non-finite counts -> zero command", bad_counts.linear_mps == 0.0, bad_counts)
    check("non-finite counts status is STALE",
          bad_counts.status is fc.ChassisStatus.STALE, bad_counts.status)

    ctl = fc.ChassisVelocityController(cfg, cal)
    warm = ctl.step(100.0, 0.1, 0.0, counts(100.0, 552, 662))
    check("first sample only warms up", warm.status is fc.ChassisStatus.WARMUP, warm.status)
    check("warmup commands zero", warm.linear_mps == 0.0, warm.linear_mps)
    check("warmup has no ttl", warm.ttl_s == 0.0, warm.ttl_s)

    hold = ctl.step(100.1, 0.08, 0.0, counts(100.1, 560, 670))
    check("8 counts in 0.1 s is 0.08 m/s", approx(hold.measured_linear_mps, 0.08),
          hold.measured_linear_mps)
    check("on-target feedback holds", hold.status is fc.ChassisStatus.HOLD, hold.status)
    check("hold command equals target", approx(hold.linear_mps, 0.08), hold.linear_mps)
    check("hold carries a positive ttl", hold.ttl_s > 0.0, hold.ttl_s)
    check("hold names its source", hold.source == "wheel_0x311", hold.source)

    ctl = fc.ChassisVelocityController(cfg, cal)
    ctl.step(100.0, 0.08, 0.0, counts(100.0, 0, 0))
    slow = ctl.step(100.1, 0.08, 0.0, counts(100.1, 2, 2))
    check("below-target feedback -> MOVING", slow.status is fc.ChassisStatus.MOVING, slow.status)
    check("below-target command is forward", slow.linear_mps > 0.0, slow.linear_mps)
    check("first command respects the step limit", slow.linear_mps <= 0.020001, slow.linear_mps)
    check("command respects the speed cap", slow.linear_mps <= cfg.max_linear_mps, slow.linear_mps)

    rev_ctl = fc.ChassisVelocityController(cfg, cal)
    rev_ctl.step(100.0, -0.05, 0.0, counts(100.0, 100, 100))
    rev = rev_ctl.step(100.1, -0.05, 0.0, counts(100.1, 98, 98))
    check("negative delta -> negative measured speed", rev.measured_linear_mps < 0, rev)
    check("reverse target commands a negative speed", rev.linear_mps < 0, rev.linear_mps)
    check("reverse command respects the step limit", abs(rev.linear_mps) <= 0.020001, rev.linear_mps)

    ctl = fc.ChassisVelocityController(cfg, cal)
    ctl.step(100.0, 0.1, 0.0, counts(100.0, 0, 0))
    zero = ctl.step(100.1, 0.1, 0.0, counts(100.1, 0, 0))
    check("stuck counts while commanded -> zero speed", zero.linear_mps == 0.0, zero)
    check("stuck counts status is ZERO_BUT_COMMANDED",
          zero.status is fc.ChassisStatus.ZERO_BUT_COMMANDED, zero.status)
    check("stuck counts command has no ttl", zero.ttl_s == 0.0, zero.ttl_s)

    ctl = fc.ChassisVelocityController(cfg, cal)
    ctl.step(100.0, 0.1, 0.0, counts(100.0, 0, 0))
    jumped = ctl.step(100.1, 0.1, 0.0, counts(100.1, 600, 600))
    check("count jump -> zero speed", jumped.linear_mps == 0.0, jumped)
    check("count jump status is JUMP", jumped.status is fc.ChassisStatus.JUMP, jumped.status)
    jump_recovered = ctl.step(100.2, 0.08, 0.0, counts(100.2, 8, 8))
    check("jump frame does not poison the baseline",
          jump_recovered.status is fc.ChassisStatus.MOVING, jump_recovered.status)
    check("loop recovers over the longer interval",
          approx(jump_recovered.measured_linear_mps, 0.04),
          jump_recovered.measured_linear_mps)
    check("recovered command is forward", jump_recovered.linear_mps > 0.0,
          jump_recovered.linear_mps)

    ctl = fc.ChassisVelocityController(cfg, cal)
    ctl.step(100.0, 0.1, 0.0, counts(100.0, 0, 0))
    fast = ctl.step(100.1, 0.1, 0.0, counts(100.1, 30, 30))
    check("overspeed feedback -> zero speed", fast.linear_mps == 0.0, fast)
    check("overspeed status is OUT_OF_RANGE",
          fast.status is fc.ChassisStatus.OUT_OF_RANGE, fast.status)

    ctl = fc.ChassisVelocityController(cfg, None)
    uncal = ctl.step(100.1, 0.1, 0.0, counts(100.1, 100, 100))
    check("uncalibrated loop -> zero speed", uncal.linear_mps == 0.0, uncal)
    check("uncalibrated loop status is UNCALIBRATED",
          uncal.status is fc.ChassisStatus.UNCALIBRATED, uncal.status)
    check("uncalibrated loop reports no metres", uncal.measured_linear_mps is None,
          uncal.measured_linear_mps)

    ctl = fc.ChassisVelocityController(cfg, cal)
    ctl.step(100.0, 0.1, 0.0, counts(100.0, 0, 0))
    stale = ctl.step(100.5, 0.1, 0.0, counts(100.1, 10, 10))
    check("stale sample -> zero speed", stale.linear_mps == 0.0, stale)
    check("stale sample status is STALE", stale.status is fc.ChassisStatus.STALE, stale.status)

    ctl = fc.ChassisVelocityController(cfg, cal)
    ctl.step(100.0, 0.08, 0.0, counts(100.0, 0, 0))
    future = ctl.step(100.1, 0.08, 0.0, counts(101.0, 8, 8))
    check("future sample -> zero speed", future.linear_mps == 0.0, future)
    check("future sample status is FUTURE", future.status is fc.ChassisStatus.FUTURE,
          future.status)
    recovered = ctl.step(100.2, 0.08, 0.0, counts(100.1, 8, 8))
    check("future sample does not poison the baseline",
          recovered.status is fc.ChassisStatus.HOLD, recovered.status)
    check("recovered loop measures the real delta",
          approx(recovered.measured_linear_mps, 0.08), recovered.measured_linear_mps)

    ctl = fc.ChassisVelocityController(cfg, cal)
    ctl.step(100.0, 0.08, 0.0, counts(100.0, 0, 0))
    wrong_clock = ctl.step(100.1, 0.08, 0.0, counts(100.1, 8, 8, clock="epoch"))
    check("clock mismatch -> zero speed", wrong_clock.linear_mps == 0.0, wrong_clock)
    check("clock mismatch status is CLOCK_MISMATCH",
          wrong_clock.status is fc.ChassisStatus.CLOCK_MISMATCH, wrong_clock.status)
    clock_recovered = ctl.step(100.2, 0.08, 0.0, counts(100.2, 16, 16))
    check("clock mismatch recovers on a valid sample",
          clock_recovered.status is fc.ChassisStatus.HOLD, clock_recovered.status)

    ctl = fc.ChassisVelocityController(cfg, cal)
    ctl.step(100.0, 0.08, 0.0, counts(100.0, 0, 0))
    unknown_clock = ctl.step(100.1, 0.08, 0.0, counts(100.1, 8, 8, clock=None))
    check("unknown clock -> zero speed", unknown_clock.linear_mps == 0.0, unknown_clock)
    check("unknown clock status is CLOCK_UNKNOWN",
          unknown_clock.status is fc.ChassisStatus.CLOCK_UNKNOWN, unknown_clock.status)

    epoch_cfg = fc.ChassisControlConfig(
        clock="epoch", gate=fc.FeedbackGateConfig(clock="epoch"))
    epoch_ctl = fc.ChassisVelocityController(epoch_cfg, cal)
    epoch_ctl.step(100.0, 0.08, 0.0, counts(100.0, 0, 0, clock="epoch"))
    epoch_hold = epoch_ctl.step(100.1, 0.08, 0.0,
                                counts(100.1, 8, 8, clock="epoch"))
    check("epoch clock domain closes the loop",
          epoch_hold.status is fc.ChassisStatus.HOLD, epoch_hold.status)
    check("controller/gate clock mismatch rejected",
          raises(ValueError, fc.ChassisControlConfig.validate,
                 fc.ChassisControlConfig(clock="epoch")))

    ctl = fc.ChassisVelocityController(cfg, cal)
    ctl.step(100.0, 0.08, 0.0, counts(100.0, 0, 0))
    long_gap = ctl.step(120.0, 0.08, 0.0, counts(120.0, 80, 80))
    check("long gap -> zero speed", long_gap.linear_mps == 0.0, long_gap)
    check("long gap status is STALE", long_gap.status is fc.ChassisStatus.STALE,
          long_gap.status)
    gap_recovered = ctl.step(120.1, 0.08, 0.0, counts(120.1, 88, 88))
    check("long gap recovers after a fresh sample",
          gap_recovered.status is fc.ChassisStatus.HOLD, gap_recovered.status)

    ctl = fc.ChassisVelocityController(cfg, cal)
    ctl.step(100.0, 0.08, 0.0, counts(100.0, 2 ** 31 - 5, 2 ** 31 - 5))
    wrapped_hold = ctl.step(
        100.1, 0.08, 0.0, counts(100.1, -(2 ** 31) + 3, -(2 ** 31) + 3))
    check("int32 wrap closes the loop", wrapped_hold.status is fc.ChassisStatus.HOLD,
          wrapped_hold.status)
    check("int32 wrap measures 0.08 m/s",
          approx(wrapped_hold.measured_linear_mps, 0.08),
          wrapped_hold.measured_linear_mps)

    ctl = fc.ChassisVelocityController(cfg, cal)
    ctl.emergency_stop()
    stop = ctl.step(100.0, 0.1, 0.0, counts(100.0, 0, 0))
    check("emergency stop -> zero speed", stop.linear_mps == 0.0 and stop.angular_rps == 0.0, stop)
    check("emergency stop status is ESTOP", stop.status is fc.ChassisStatus.ESTOP, stop.status)

    ctl = fc.ChassisVelocityController(cfg, cal)
    ctl.step(100.0, 0.0, 0.0, counts(100.0, 0, 0))
    turn = ctl.step(100.1, 0.0, 0.1, counts(100.1, -2, 2))
    check("pure turn has zero linear speed", approx(turn.measured_linear_mps, 0.0),
          turn.measured_linear_mps)
    check("pure turn measures 0.1 rad/s", approx(turn.measured_angular_rps, 0.1),
          turn.measured_angular_rps)
    check("pure turn on target holds", turn.status is fc.ChassisStatus.HOLD, turn.status)

    ctl = fc.ChassisVelocityController(cfg, cal)
    ctl.step(100.0, 0.1, 0.0, counts(100.0, 0, 0))
    stopped = ctl.step(100.1, 0.0, 0.0, counts(100.1, 0, 0))
    check("zero target with zero feedback holds", stopped.status is fc.ChassisStatus.HOLD,
          stopped.status)
    check("zero target commands zero", stopped.linear_mps == 0.0, stopped.linear_mps)

    pulse_cal = fc.WheelCalibration(
        10000.0, 0.4, source="actuator_pulse", pulse_mapping=PULSE_MAP)
    ctl = fc.ChassisVelocityController(cfg, pulse_cal)
    ctl.step(100.0, 0.08, 0.0,
             counts(100.0, 0, 0, source="actuator_pulse", mapping=PULSE_MAP))
    pulse_hold = ctl.step(100.1, 0.08, 0.0,
                          counts(100.1, 80, 80, source="actuator_pulse",
                                 mapping=PULSE_MAP))
    check("actuator pulse source closes the loop", pulse_hold.status is fc.ChassisStatus.HOLD,
          pulse_hold.status)
    check("actuator pulse source is labelled", pulse_hold.source == "actuator_pulse",
          pulse_hold.source)

    for bad_cfg in (
        fc.ChassisControlConfig(max_linear_mps=0.0),
        fc.ChassisControlConfig(max_angular_rps=0.0),
        fc.ChassisControlConfig(command_ttl_s=0.0),
        fc.ChassisControlConfig(max_linear_step_mps=0.0),
        fc.ChassisControlConfig(max_angular_step_rps=0.0),
        fc.ChassisControlConfig(clock=""),
    ):
        check("invalid chassis config rejected",
              raises(ValueError, fc.ChassisControlConfig.validate, bad_cfg))


# ---------------------------------------------------------------------------
# 7. Arm small-step loop
# ---------------------------------------------------------------------------
def arm_fb(now, joints, mode="MOVE_J", fault=None, enabled=True, lag=0.0,
           ctrl_mode=1, clock="monotonic"):
    return fc.ArmFeedback(
        timestamp=now,
        joints_deg=tuple(float(v) for v in joints),
        mode=mode,
        fault=fault,
        enabled=enabled,
        lag_deg=lag,
        ctrl_mode=ctrl_mode,
        clock=clock,
    )


def apply_plant(plant, target, factor=1.0, extra=0.0):
    for i, (m, t) in enumerate(zip(plant, target)):
        d = (t - m) * factor
        if d > 0:
            d += extra
        elif d < 0:
            d -= extra
        plant[i] = m + d


def drive(ctl, plant, target, now0=0.0, dt=0.01, limit=400, factor=1.0, extra=0.0):
    log = []
    now = now0
    for _ in range(limit):
        fb = arm_fb(now, plant)
        cmd = ctl.step(now, fb)
        log.append((now, tuple(plant), cmd))
        if cmd.status in (
            fc.ArmStatus.CONVERGED,
            fc.ArmStatus.STALLED,
            fc.ArmStatus.TIMEOUT,
            fc.ArmStatus.MAX_ITERATIONS,
            fc.ArmStatus.OVERSHOOT,
            fc.ArmStatus.FAULT,
            fc.ArmStatus.MODE,
            fc.ArmStatus.FEEDBACK_LOST,
            fc.ArmStatus.UNREACHABLE,
            fc.ArmStatus.ESTOP,
        ):
            break
        apply_plant(plant, cmd.target_joints_deg, factor, extra)
        now += dt
    return log


def step_caps_ok(log):
    for _, measured, cmd in log:
        if cmd.step_deg > 0.5 + 1e-9:
            return False
        for m, t in zip(measured, cmd.target_joints_deg):
            if abs(t - m) > 0.5 + 1e-9:
                return False
    return True


def test_arm_loop():
    print("== arm small-step loop ==")
    cfg = fc.ArmControlConfig()
    check("arm default max step is 0.5 deg", cfg.max_step_deg == 0.5, cfg.max_step_deg)

    ctl = fc.JointSetpointController([1.0, 0, 0, 0, 0, 0], cfg)
    plant = [0.0] * 6
    log = drive(ctl, plant, target=1.0)
    check("single joint reaches CONVERGED", log[-1][2].status is fc.ArmStatus.CONVERGED,
          log[-1][2].status)
    check("single joint every step <= 0.5 deg", step_caps_ok(log))
    check("single joint final error within tolerance",
          log[-1][2].error_deg <= cfg.tolerance_deg + 1e-9, log[-1][2].error_deg)
    check("converged command equals target",
          all(approx(a, b) for a, b in zip(log[-1][2].target_joints_deg, [1.0, 0, 0, 0, 0, 0])),
          log[-1][2].target_joints_deg)

    ctl = fc.JointSetpointController([-20.0, 30.0, -40.0, 10.0, 5.0, -5.0], cfg)
    plant = [0.0] * 6
    log = drive(ctl, plant, target=None, limit=600)
    check("six joints reach CONVERGED", log[-1][2].status is fc.ArmStatus.CONVERGED,
          log[-1][2].status)
    check("six joints every step <= 0.5 deg", step_caps_ok(log))
    check("reverse joints moved negative", plant[0] < 0 and plant[2] < 0, plant)

    ctl = fc.JointSetpointController([-1.0, 0, 0, 0, 0, 0], cfg)
    plant = [0.0] * 6
    log = drive(ctl, plant, target=-1.0)
    check("reverse single joint converges", log[-1][2].status is fc.ArmStatus.CONVERGED,
          log[-1][2].status)
    check("reverse commands are negative",
          all(c.target_joints_deg[0] < 0 for _, _, c in log[:3]), log[:3])

    settle_cfg = fc.ArmControlConfig(tolerance_deg=0.15, settle_steps=2)
    ctl = fc.JointSetpointController([0.1, 0, 0, 0, 0, 0], settle_cfg)
    first = ctl.step(0.0, arm_fb(0.0, [0.0] * 6))
    second = ctl.step(0.01, arm_fb(0.01, [0.1, 0, 0, 0, 0, 0]))
    check("first in-tolerance sample only settles", first.status is fc.ArmStatus.MOVING,
          first.status)
    check("second in-tolerance sample converges", second.status is fc.ArmStatus.CONVERGED,
          second.status)

    stale_cfg = fc.ArmControlConfig(max_feedback_age_s=0.05)
    ctl = fc.JointSetpointController([0.5, 0, 0, 0, 0, 0], stale_cfg)
    stale = ctl.step(1.0, arm_fb(0.5, [0.0] * 6))
    check("stale joint feedback -> FEEDBACK_LOST",
          stale.status is fc.ArmStatus.FEEDBACK_LOST, stale.status)
    future = ctl.step(1.0, arm_fb(1.5, [0.0] * 6))
    check("future joint feedback -> FEEDBACK_LOST",
          future.status is fc.ArmStatus.FEEDBACK_LOST, future.status)
    lost = ctl.step(1.0, None)
    check("missing joint feedback -> FEEDBACK_LOST",
          lost.status is fc.ArmStatus.FEEDBACK_LOST, lost.status)
    check("first FEEDBACK_LOST returns no target", lost.target_joints_deg == (),
          lost.target_joints_deg)
    check("first FEEDBACK_LOST is not the goal",
          lost.target_joints_deg != (0.5, 0.0, 0.0, 0.0, 0.0, 0.0),
          lost.target_joints_deg)

    nan_time = fc.JointSetpointController([1.0, 0, 0, 0, 0, 0], cfg).step(
        1.0, arm_fb(float("nan"), [0.0] * 6))
    check("non-finite feedback timestamp -> FEEDBACK_LOST",
          nan_time.status is fc.ArmStatus.FEEDBACK_LOST, nan_time.status)
    check("non-finite timestamp still yields a finite hold",
          all(math.isfinite(v) for v in nan_time.target_joints_deg),
          nan_time.target_joints_deg)

    nan_joints = fc.JointSetpointController([1.0, 0, 0, 0, 0, 0], cfg).step(
        1.0, arm_fb(1.0, [float("nan")] + [0.0] * 5))
    check("non-finite joint feedback -> FEEDBACK_LOST",
          nan_joints.status is fc.ArmStatus.FEEDBACK_LOST, nan_joints.status)
    check("non-finite joints still yield a finite hold",
          all(math.isfinite(v) for v in nan_joints.target_joints_deg),
          nan_joints.target_joints_deg)

    ctl = fc.JointSetpointController([0.5, 0, 0, 0, 0, 0], cfg)
    five = ctl.step(0.0, arm_fb(0.0, [0.0] * 5))
    check("joint count mismatch -> FEEDBACK_LOST",
          five.status is fc.ArmStatus.FEEDBACK_LOST, five.status)

    ctl = fc.JointSetpointController([0.5, 0, 0, 0, 0, 0], cfg)
    fault = ctl.step(0.0, arm_fb(0.0, [0.0] * 6, fault="collision"))
    check("arm fault -> FAULT", fault.status is fc.ArmStatus.FAULT, fault.status)
    check("fault holds the measured pose", fault.target_joints_deg == (0.0,) * 6,
          fault.target_joints_deg)
    check("fault step is zero", fault.step_deg == 0.0, fault.step_deg)

    disabled = ctl.step(0.0, arm_fb(0.0, [0.0] * 6, enabled=False))
    check("arm disabled -> DISABLED", disabled.status is fc.ArmStatus.DISABLED,
          disabled.status)
    unknown_enable = ctl.step(0.0, arm_fb(0.0, [0.0] * 6, enabled=None))
    check("unknown enable -> DISABLED", unknown_enable.status is fc.ArmStatus.DISABLED,
          unknown_enable.status)
    check("unknown enable holds measured",
          unknown_enable.target_joints_deg == (0.0,) * 6,
          unknown_enable.target_joints_deg)

    mode = ctl.step(0.0, arm_fb(0.0, [0.0] * 6, mode="MOVE_C"))
    check("unexpected arm mode -> MODE", mode.status is fc.ArmStatus.MODE, mode.status)
    mode = fc.JointSetpointController([0.5, 0, 0, 0, 0, 0], cfg).step(
        0.0, arm_fb(0.0, [0.0] * 6, ctrl_mode=2))
    check("teaching ctrl_mode -> MODE", mode.status is fc.ArmStatus.MODE, mode.status)

    lag = fc.JointSetpointController([0.5, 0, 0, 0, 0, 0], cfg).step(
        0.0, arm_fb(0.0, [0.0] * 6, lag=2.0))
    check("arm lag above cap -> LAG", lag.status is fc.ArmStatus.LAG, lag.status)
    unknown_lag = fc.JointSetpointController([0.5, 0, 0, 0, 0, 0], cfg).step(
        0.0, arm_fb(0.0, [0.0] * 6, lag=None))
    check("unknown lag -> LAG", unknown_lag.status is fc.ArmStatus.LAG, unknown_lag.status)
    check("unknown lag holds measured",
          unknown_lag.target_joints_deg == (0.0,) * 6,
          unknown_lag.target_joints_deg)

    wrong_clock = fc.JointSetpointController([0.5, 0, 0, 0, 0, 0], cfg).step(
        0.0, arm_fb(0.0, [0.0] * 6, clock="epoch"))
    check("arm clock mismatch -> CLOCK_MISMATCH",
          wrong_clock.status is fc.ArmStatus.CLOCK_MISMATCH, wrong_clock.status)
    no_clock = fc.JointSetpointController([0.5, 0, 0, 0, 0, 0], cfg).step(
        0.0, arm_fb(0.0, [0.0] * 6, clock=None))
    check("arm clock unknown -> CLOCK_UNKNOWN",
          no_clock.status is fc.ArmStatus.CLOCK_UNKNOWN, no_clock.status)
    epoch_arm = fc.JointSetpointController(
        [0.5, 0, 0, 0, 0, 0], fc.ArmControlConfig(clock="epoch")).step(
        0.0, arm_fb(0.0, [0.0] * 6, clock="epoch"))
    check("arm epoch clock closes the loop",
          epoch_arm.status is fc.ArmStatus.MOVING, epoch_arm.status)

    after_step = fc.JointSetpointController([2.0, 0, 0, 0, 0, 0], cfg)
    first_move = after_step.step(0.0, arm_fb(0.0, [1.0, 0, 0, 0, 0, 0]))
    check("first move succeeds", first_move.status is fc.ArmStatus.MOVING,
          first_move.status)
    lost_after_move = after_step.step(0.01, None)
    check("lost feedback holds the last measured pose",
          lost_after_move.target_joints_deg == (1.0, 0.0, 0.0, 0.0, 0.0, 0.0),
          lost_after_move.target_joints_deg)

    unreachable = fc.JointSetpointController([200.0, 0, 0, 0, 0, 0], cfg).step(
        0.0, arm_fb(0.0, [0.0] * 6))
    check("target outside joint limits -> UNREACHABLE",
          unreachable.status is fc.ArmStatus.UNREACHABLE, unreachable.status)

    ctl = fc.JointSetpointController([10.0, 0, 0, 0, 0, 0], cfg)
    plant = [0.0] * 6
    log = drive(ctl, plant, target=10.0, factor=0.0)
    check("frozen plant -> STALLED", log[-1][2].status is fc.ArmStatus.STALLED,
          log[-1][2].status)
    check("stall reports iterations", "no movement" in log[-1][2].reason,
          log[-1][2].reason)

    timeout_cfg = fc.ArmControlConfig(timeout_s=0.05, stall_iterations=100,
                                      tolerance_deg=0.01)
    ctl = fc.JointSetpointController([10.0, 0, 0, 0, 0, 0], timeout_cfg)
    plant = [0.0] * 6
    log = drive(ctl, plant, target=10.0, dt=0.02, factor=0.0)
    check("deadline exceeded -> TIMEOUT", log[-1][2].status is fc.ArmStatus.TIMEOUT,
          log[-1][2].status)

    iter_cfg = fc.ArmControlConfig(max_iterations=2, timeout_s=1000.0,
                                   stall_iterations=100, tolerance_deg=0.01)
    ctl = fc.JointSetpointController([10.0, 0, 0, 0, 0, 0], iter_cfg)
    plant = [0.0] * 6
    log = drive(ctl, plant, target=10.0, factor=0.01)
    check("iteration budget -> MAX_ITERATIONS",
          log[-1][2].status is fc.ArmStatus.MAX_ITERATIONS, log[-1][2].status)

    overshoot_cfg = fc.ArmControlConfig(tolerance_deg=0.01, max_overshoots=0)
    ctl = fc.JointSetpointController([10.0, 0, 0, 0, 0, 0], overshoot_cfg)
    plant = [0.0] * 6
    log = drive(ctl, plant, target=10.0, factor=2.4)
    check("overshoot budget -> OVERSHOOT", log[-1][2].status is fc.ArmStatus.OVERSHOOT,
          log[-1][2].status)
    check("overshoot still respects the step cap", step_caps_ok(log))

    damp_cfg = fc.ArmControlConfig(tolerance_deg=0.01, max_overshoots=5)
    ctl = fc.JointSetpointController([10.0, 0, 0, 0, 0, 0], damp_cfg)
    plant = [0.0] * 6
    log = drive(ctl, plant, target=10.0, factor=2.4)
    check("overshoot halves the gain", ctl.gain < damp_cfg.gain, ctl.gain)
    check("damped loop keeps stepping", step_caps_ok(log))

    ctl = fc.JointSetpointController([10.0, 0, 0, 0, 0, 0], cfg)
    ctl.step(0.0, arm_fb(0.0, [1.0, 0, 0, 0, 0, 0]))
    ctl.emergency_stop()
    estop = ctl.step(0.01, arm_fb(0.01, [1.0, 0, 0, 0, 0, 0]))
    check("arm emergency stop -> ESTOP", estop.status is fc.ArmStatus.ESTOP, estop.status)
    check("arm emergency stop holds measured pose", estop.target_joints_deg[0] == 1.0,
          estop.target_joints_deg)
    check("arm emergency stop step is zero", estop.step_deg == 0.0, estop.step_deg)

    # Real left-arm pose from the passive audit: J2=-2.06, J3=+1.44.
    real_pose = [20.204, -2.06, 1.44, -61.832, 28.471, 40.604]
    margin_target = [20.204, 0.5, -5.0, -61.832, 28.471, 40.604]
    margin_ctl = fc.JointSetpointController(margin_target, cfg)
    margin_cmd = margin_ctl.step(0.0, arm_fb(0.0, real_pose))
    check("real out-of-limit J2/J3 can move inward",
          margin_cmd.status is fc.ArmStatus.MOVING, margin_cmd.status)
    check("margin keeps J2 at or above measured",
          margin_cmd.target_joints_deg[1] >= real_pose[1],
          margin_cmd.target_joints_deg[1])
    check("margin keeps J3 at or below measured",
          margin_cmd.target_joints_deg[2] <= real_pose[2],
          margin_cmd.target_joints_deg[2])
    check("margin step still <= 0.5 deg", margin_cmd.step_deg <= 0.5 + 1e-9,
          margin_cmd.step_deg)

    plant = list(real_pose)
    log = drive(margin_ctl, plant, target=None, limit=200)
    check("real pose converges after recentring",
          log[-1][2].status is fc.ArmStatus.CONVERGED, log[-1][2].status)
    check("recentring never commands J2 past measured",
          all(c.target_joints_deg[1] >= m[1] - 1e-9 for _, m, c in log),
          [(m[1], c.target_joints_deg[1]) for _, m, c in log[-3:]])
    check("recentring never commands J3 further out",
          all(c.target_joints_deg[2] <= m[2] + 1e-9 for _, m, c in log),
          [(m[2], c.target_joints_deg[2]) for _, m, c in log[-3:]])

    strict_ctl = fc.JointSetpointController(
        margin_target, fc.ArmControlConfig(limit_margin_deg=0.0))
    strict_cmd = strict_ctl.step(0.0, arm_fb(0.0, real_pose))
    check("zero margin refuses the real out-of-limit pose",
          strict_cmd.status is fc.ArmStatus.SAFETY, strict_cmd.status)

    far_pose = list(real_pose)
    far_pose[1] = -100.0
    far_cmd = fc.JointSetpointController(margin_target, cfg).step(
        0.0, arm_fb(0.0, far_pose))
    check("pose beyond the margin -> SAFETY", far_cmd.status is fc.ArmStatus.SAFETY,
          far_cmd.status)

    outside_target = list(real_pose)
    outside_target[1] = -1.0
    outside_cmd = fc.JointSetpointController(outside_target, cfg).step(
        0.0, arm_fb(0.0, real_pose))
    check("target outside nominal limits -> UNREACHABLE",
          outside_cmd.status is fc.ArmStatus.UNREACHABLE, outside_cmd.status)

    for bad_cfg in (
        fc.ArmControlConfig(max_step_deg=0.6),
        fc.ArmControlConfig(max_step_deg=0.0),
        fc.ArmControlConfig(tolerance_deg=0.0),
        fc.ArmControlConfig(tolerance_deg=1.0),
        fc.ArmControlConfig(settle_steps=0),
        fc.ArmControlConfig(timeout_s=0.0),
        fc.ArmControlConfig(max_iterations=0),
        fc.ArmControlConfig(gain=0.0),
        fc.ArmControlConfig(gain=1.5),
        fc.ArmControlConfig(stall_iterations=0),
        fc.ArmControlConfig(max_overshoots=-1),
        fc.ArmControlConfig(joint_limits_deg=((-1.0, 1.0),)),
        fc.ArmControlConfig(limit_margin_deg=-1.0),
        fc.ArmControlConfig(clock=""),
    ):
        check("invalid arm config rejected",
              raises(ValueError, fc.ArmControlConfig.validate, bad_cfg))

    check("controller rejects 0 joints",
          raises(ValueError, fc.JointSetpointController, []))
    check("controller rejects 7 joints",
          raises(ValueError, fc.JointSetpointController, [0.0] * 7))
    check("controller rejects a non-finite target",
          raises(ValueError, fc.JointSetpointController, [float("nan")]))


# ---------------------------------------------------------------------------
# 8. Piper frame assembly and real-frame integration
# ---------------------------------------------------------------------------
def test_arm_assembly():
    print("== Piper feedback assembly ==")
    asm = fc.ArmFeedbackAssembler()
    check("empty assembler has no snapshot", asm.snapshot() is None)

    asm.feed(parse(fc.CAN_ID_ARM_JOINT_12, "00004EEC00000000", timestamp=100.0))
    check("partial joint set has no snapshot", asm.snapshot() is None)

    asm.feed(parse(fc.CAN_ID_ARM_JOINT_34, "FFFFEC22FFFF0E78", timestamp=100.0))
    asm.feed(parse(fc.CAN_ID_ARM_JOINT_56, "00006F3700009E9C", timestamp=100.0))
    check("joints without status still refuse a snapshot", asm.snapshot() is None)

    asm.feed(parse(fc.CAN_ID_ARM_STATUS, "0100010200000000", timestamp=100.0))
    snap = asm.snapshot()
    check("assembled snapshot exists", snap is not None, snap)
    check("assembled J1", approx(snap.joints_deg[0], 20.204), snap.joints_deg)
    check("assembled J4", approx(snap.joints_deg[3], -61.832), snap.joints_deg)
    check("assembled J6", approx(snap.joints_deg[5], 40.604), snap.joints_deg)
    check("assembled mode is MOVE_J", snap.mode == "MOVE_J", snap.mode)
    check("assembled ctrl_mode is 1", snap.ctrl_mode == 1, snap.ctrl_mode)
    check("assembled normal frame has no fault", snap.fault is None, snap.fault)
    check("assembled feedback age is zero", approx(snap.age(100.0), 0.0), snap.age(100.0))
    check("assembled feedback age grows", approx(snap.age(100.2), 0.2), snap.age(100.2))
    check("assembled feedback does not fake enabled", snap.enabled is None, snap.enabled)
    check("assembled feedback does not fake lag", snap.lag_deg is None, snap.lag_deg)
    check("assembled feedback keeps the clock", snap.clock == "monotonic", snap.clock)
    check("assembled feedback keeps the bus", snap.iface == "can_left", snap.iface)

    default_cmd = fc.JointSetpointController(
        [20.604, 0, -5, -61, 28, 40], fc.ArmControlConfig()).step(100.0, snap)
    check("assembler default enable state is rejected",
          default_cmd.status is fc.ArmStatus.DISABLED, default_cmd.status)
    check("assembler default holds measured, not target",
          default_cmd.target_joints_deg == snap.joints_deg,
          default_cmd.target_joints_deg)

    no_lag = asm.snapshot(enabled=True)
    check("assembler does not fake a zero lag", no_lag.lag_deg is None, no_lag.lag_deg)
    no_lag_cmd = fc.JointSetpointController(
        [20.604, 0, -5, -61, 28, 40], fc.ArmControlConfig()).step(100.0, no_lag)
    check("unknown lag is rejected", no_lag_cmd.status is fc.ArmStatus.LAG,
          no_lag_cmd.status)

    disabled = asm.snapshot(enabled=False, lag_deg=0.0)
    disabled_cmd = fc.JointSetpointController(
        [20.604, 0, -5, -61, 28, 40], fc.ArmControlConfig()).step(100.0, disabled)
    check("assembler external disabled is honoured",
          disabled_cmd.status is fc.ArmStatus.DISABLED, disabled_cmd.status)

    enabled = asm.snapshot(enabled=True, lag_deg=0.0)
    enabled_cmd = fc.JointSetpointController(
        [20.604, 0, -5, -61, 28, 40], fc.ArmControlConfig()).step(100.0, enabled)
    check("assembler external enabled allows MOVING",
          enabled_cmd.status is fc.ArmStatus.MOVING, enabled_cmd.status)

    lagged = asm.snapshot(enabled=True, lag_deg=2.0)
    lagged_cmd = fc.JointSetpointController(
        [20.604, 0, -5, -61, 28, 40], fc.ArmControlConfig()).step(100.0, lagged)
    check("assembler external lag is honoured",
          lagged_cmd.status is fc.ArmStatus.LAG, lagged_cmd.status)

    adapted_target = list(snap.joints_deg)
    adapted_target[0] += 0.5
    adapted = asm.snapshot(enabled=True, target_joints_deg=adapted_target)
    check("assembler derives lag from an external target",
          approx(adapted.lag_deg, 0.5), adapted.lag_deg)
    adapted_cmd = fc.JointSetpointController(
        [20.604, 0, -5, -61, 28, 40], fc.ArmControlConfig()).step(100.0, adapted)
    check("derived lag under the cap is accepted",
          adapted_cmd.status is fc.ArmStatus.MOVING, adapted_cmd.status)

    far_target = list(snap.joints_deg)
    far_target[0] += 2.0
    far = asm.snapshot(enabled=True, target_joints_deg=far_target)
    far_cmd = fc.JointSetpointController(
        [20.604, 0, -5, -61, 28, 40], fc.ArmControlConfig()).step(100.0, far)
    check("derived lag above the cap is rejected",
          far_cmd.status is fc.ArmStatus.LAG, far_cmd.status)

    check("assembler rejects a non-bool enabled",
          raises(ValueError, asm.snapshot, enabled="yes"))
    check("assembler rejects a negative lag",
          raises(ValueError, asm.snapshot, enabled=True, lag_deg=-1.0))
    check("assembler rejects a short target",
          raises(ValueError, asm.snapshot, enabled=True,
                 target_joints_deg=[0.0] * 5))
    check("assembler rejects a non-finite target",
          raises(ValueError, asm.snapshot, enabled=True,
                 target_joints_deg=[float("nan")] + [0.0] * 5))
    check("assembler rejects a different expected clock",
          asm.snapshot(enabled=True, lag_deg=0.0, expected_clock="epoch") is None)

    asm2 = fc.ArmFeedbackAssembler()
    asm2.feed(parse(fc.CAN_ID_ARM_JOINT_56, "00006F3700009E9C", timestamp=101.0))
    asm2.feed(parse(fc.CAN_ID_ARM_STATUS, "0100010200000000", timestamp=101.0))
    asm2.feed(parse(fc.CAN_ID_ARM_JOINT_12, "00004EEC00000000", timestamp=101.0))
    asm2.feed(parse(fc.CAN_ID_ARM_JOINT_34, "FFFFEC22FFFF0E78", timestamp=101.0))
    check("out-of-order frames still assemble", asm2.snapshot() is not None)

    asm6 = fc.ArmFeedbackAssembler()
    asm6.feed(parse(fc.CAN_ID_ARM_JOINT_12, "00004EEC00000000", timestamp=100.0))
    asm6.feed(parse(fc.CAN_ID_ARM_JOINT_34, "FFFFEC22FFFF0E78", timestamp=100.0))
    asm6.feed(parse(fc.CAN_ID_ARM_JOINT_56, "00006F3700009E9C", timestamp=100.0))
    asm6.feed(parse(fc.CAN_ID_ARM_STATUS, "0100010200000000", timestamp=90.0))
    stale_snap = asm6.snapshot()
    check("stale status frame is not masked by fresh joints",
          stale_snap is not None and approx(stale_snap.timestamp, 90.0),
          stale_snap.timestamp if stale_snap else None)
    stale_cmd = fc.JointSetpointController(
        [20.204, 0, -5.086, -61.832, 28.471, 40.604], fc.ArmControlConfig()
    ).step(100.0, stale_snap)
    check("stale status aborts the arm loop",
          stale_cmd.status is fc.ArmStatus.FEEDBACK_LOST, stale_cmd.status)

    asm7 = fc.ArmFeedbackAssembler()
    asm7.feed(parse(fc.CAN_ID_ARM_JOINT_12, "00004EEC00000000", timestamp=100.0))
    asm7.feed(parse(fc.CAN_ID_ARM_JOINT_34, "FFFFEC22FFFF0E78", timestamp=100.0))
    asm7.feed(parse(fc.CAN_ID_ARM_JOINT_56, "00006F3700009E9C", timestamp=100.0))
    asm7.feed(parse(fc.CAN_ID_ARM_STATUS, "0100010200000000"))
    check("status without a timestamp refuses a snapshot", asm7.snapshot() is None)

    asm8 = fc.ArmFeedbackAssembler()
    asm8.feed(parse(fc.CAN_ID_ARM_JOINT_12, "00004EEC00000000", timestamp=110.0,
                    clock="monotonic"))
    asm8.feed(parse(fc.CAN_ID_ARM_JOINT_34, "FFFFEC22FFFF0E78", timestamp=110.0,
                    clock="epoch"))
    asm8.feed(parse(fc.CAN_ID_ARM_JOINT_56, "00006F3700009E9C", timestamp=110.0,
                    clock="monotonic"))
    asm8.feed(parse(fc.CAN_ID_ARM_STATUS, "0100010200000000", timestamp=110.0,
                    clock="monotonic"))
    check("mixed arm clock domains refuse a snapshot", asm8.snapshot() is None)

    asm9 = fc.ArmFeedbackAssembler()
    asm9.feed(parse(fc.CAN_ID_ARM_JOINT_12, "00004EEC00000000", timestamp=111.0,
                    clock="monotonic"))
    asm9.feed(parse(fc.CAN_ID_ARM_JOINT_34, "FFFFEC22FFFF0E78", timestamp=111.0,
                    clock="monotonic"))
    asm9.feed(parse(fc.CAN_ID_ARM_JOINT_56, "00006F3700009E9C", timestamp=111.0,
                    clock="monotonic"))
    asm9.feed(parse(fc.CAN_ID_ARM_STATUS, "0100010200000000", timestamp=111.0,
                    iface="can_right", clock="monotonic"))
    check("mixed arm buses refuse a snapshot", asm9.snapshot() is None)

    asm10 = fc.ArmFeedbackAssembler()
    asm10.feed(parse(fc.CAN_ID_ARM_JOINT_12, "00004EEC00000000", timestamp=112.0,
                     clock=None))
    asm10.feed(parse(fc.CAN_ID_ARM_JOINT_34, "FFFFEC22FFFF0E78", timestamp=112.0,
                     clock=None))
    asm10.feed(parse(fc.CAN_ID_ARM_JOINT_56, "00006F3700009E9C", timestamp=112.0,
                     clock=None))
    asm10.feed(parse(fc.CAN_ID_ARM_STATUS, "0100010200000000", timestamp=112.0,
                     clock=None))
    check("unknown arm clock refuses a snapshot", asm10.snapshot() is None)

    # Real left-arm pose from the audit: J2=-2.06, J3=+1.44.
    real_asm = fc.ArmFeedbackAssembler()
    real_asm.feed(parse(fc.CAN_ID_ARM_JOINT_12, "00004EECFFFFF7F4", timestamp=120.0))
    real_asm.feed(parse(fc.CAN_ID_ARM_JOINT_34, "000005A0FFFF0E78", timestamp=120.0))
    real_asm.feed(parse(fc.CAN_ID_ARM_JOINT_56, "00006F3700009E9C", timestamp=120.0))
    real_asm.feed(parse(fc.CAN_ID_ARM_STATUS, "0100010200000000", timestamp=120.0))
    real_snap = real_asm.snapshot(enabled=True, lag_deg=0.0)
    check("real pose assembles J2=-2.06", approx(real_snap.joints_deg[1], -2.06),
          real_snap.joints_deg)
    check("real pose assembles J3=+1.44", approx(real_snap.joints_deg[2], 1.44),
          real_snap.joints_deg)
    real_setpoint = [20.204, -1.56, 0.94, -61.832, 28.471, 40.604]
    real_adapted = real_asm.snapshot(enabled=True,
                                     target_joints_deg=real_setpoint)
    check("real pose derives lag from a small setpoint",
          approx(real_adapted.lag_deg, 0.5), real_adapted.lag_deg)
    real_cmd = fc.JointSetpointController(
        [20.204, 0.5, -5.0, -61.832, 28.471, 40.604],
        fc.ArmControlConfig()).step(120.0, real_snap)
    check("assembled real pose can recenter", real_cmd.status is fc.ArmStatus.MOVING,
          real_cmd.status)
    check("assembled real pose moves J2 inward",
          real_cmd.target_joints_deg[1] >= -2.06, real_cmd.target_joints_deg[1])
    check("assembled real pose moves J3 inward",
          real_cmd.target_joints_deg[2] <= 1.44, real_cmd.target_joints_deg[2])

    asm3 = fc.ArmFeedbackAssembler()
    asm3.feed(parse(fc.CAN_ID_ARM_JOINT_12, "00004EEC00000000", timestamp=102.0))
    asm3.feed(parse(fc.CAN_ID_ARM_JOINT_34, "FFFFEC22FFFF0E78", timestamp=102.0))
    asm3.feed(parse(fc.CAN_ID_ARM_JOINT_56, "00006F3700009E9C", timestamp=102.0))
    asm3.feed(parse(fc.CAN_ID_ARM_STATUS, "0101010200000000", timestamp=102.0))
    faulted = asm3.snapshot()
    check("arm_status=1 becomes a fault", faulted.fault is not None, faulted.fault)

    asm4 = fc.ArmFeedbackAssembler()
    asm4.feed(parse(fc.CAN_ID_ARM_JOINT_12, "00004EEC00000000", timestamp=103.0))
    asm4.feed(parse(fc.CAN_ID_ARM_JOINT_34, "FFFFEC22FFFF0E78", timestamp=103.0))
    asm4.feed(parse(fc.CAN_ID_ARM_JOINT_56, "00006F3700009E9C", timestamp=103.0))
    asm4.feed(parse(fc.CAN_ID_ARM_STATUS, "0100010200000001", timestamp=103.0))
    comm = asm4.snapshot()
    check("err_code bit 0 becomes a fault", comm.fault is not None, comm.fault)

    asm5 = fc.ArmFeedbackAssembler()
    asm5.feed(parse(fc.CAN_ID_ARM_JOINT_12, "00004EEC00000000", timestamp=104.0))
    asm5.feed(parse(fc.CAN_ID_ARM_JOINT_34, "FFFFEC22FFFF0E78", timestamp=104.0))
    asm5.feed(parse(fc.CAN_ID_ARM_JOINT_56, "00006F3700009E9C", timestamp=104.0))
    asm5.feed(parse(fc.CAN_ID_ARM_STATUS, "0100000200000000", timestamp=104.0))
    move_p = asm5.snapshot(enabled=True, lag_deg=0.0)
    check("mode_feed=0 becomes MOVE_P", move_p.mode == "MOVE_P", move_p.mode)
    check("MOVE_P is accepted by the controller",
          fc.JointSetpointController([20.5, 0, -5, -61, 28, 40], fc.ArmControlConfig())
          .step(104.0, move_p).status is fc.ArmStatus.MOVING)

    asm5b = fc.ArmFeedbackAssembler()
    for cid, hexs in ((fc.CAN_ID_ARM_JOINT_12, "00004EEC00000000"),
                      (fc.CAN_ID_ARM_JOINT_34, "FFFFEC22FFFF0E78"),
                      (fc.CAN_ID_ARM_JOINT_56, "00006F3700009E9C"),
                      (fc.CAN_ID_ARM_STATUS, "0100010201000000")):
        asm5b.feed(parse(cid, hexs, timestamp=105.0))
    moving_snap = asm5b.snapshot(enabled=True, lag_deg=0.0)
    check("motion_status=1 is not a fault in assembler path",
          moving_snap.fault is None and moving_snap.motion_status == 1,
          (moving_snap.fault, moving_snap.motion_status))

    ctl = fc.JointSetpointController([20.604, 0, -5, -61, 28, 40], fc.ArmControlConfig())
    snap = asm.snapshot(enabled=True, lag_deg=0.0)
    cmd = ctl.step(100.0, snap)
    check("real-frame arm target -> MOVING", cmd.status is fc.ArmStatus.MOVING, cmd.status)
    check("real-frame arm step <= 0.5 deg", cmd.step_deg <= 0.5 + 1e-9, cmd.step_deg)
    check("real-frame arm J1 step is 0.2 deg",
          approx(cmd.target_joints_deg[0] - 20.204, 0.2), cmd.target_joints_deg[0])

    ctl = fc.JointSetpointController([20.604, 0, -5, -61, 28, 40], fc.ArmControlConfig())
    cmd = ctl.step(102.0, asm3.snapshot())
    check("real-frame fault aborts the loop", cmd.status is fc.ArmStatus.FAULT, cmd.status)


# ---------------------------------------------------------------------------
# 9. Module hygiene
# ---------------------------------------------------------------------------
def test_module_hygiene():
    print("== module hygiene ==")
    with open(MODULE, "r", encoding="utf-8") as handle:
        source = handle.read()
    for forbidden in ("import socket", "import piper_sdk", "import can", "import serial",
                      "import requests", "import rclpy"):
        check("module does not use %s" % forbidden, forbidden not in source)
    check("module is importable without hardware", hasattr(fc, "decode_frame"))


def main():
    test_known_frames()
    test_candump_lines()
    test_invalid_frames()
    test_calibration()
    test_gate()
    test_chassis_loop()
    test_arm_loop()
    test_arm_assembly()
    test_module_hygiene()
    print()
    if FAIL:
        print("%d FAILED: %s" % (len(FAIL), FAIL))
        return 1
    print("ALL FEEDBACK CONTROL TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
