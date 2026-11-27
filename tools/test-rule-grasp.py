#!/usr/bin/env python3
"""Offline tests for tools/rule_grasp.py.

No robot, no CAN, no ROS, no lease server and no real camera.  The only HTTP
code exercised is a fake lease object that records the bodies the transport
would have sent.

    python3 tools/test-rule-grasp.py

Stdlib only, same style as test-server-offline.py and test-feedback-control.py.
"""
import importlib.util
import json
import math
import os
import sys
import tempfile

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
MODULE = os.path.join(HERE, "rule_grasp.py")

spec = importlib.util.spec_from_file_location("rule_grasp", MODULE)
rg = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = rg
spec.loader.exec_module(rg)

FAIL = []
COUNT = [0]


def check(name, cond, detail=""):
    COUNT[0] += 1
    print(("PASS " if cond else "FAIL ") + name +
          (("  " + str(detail)) if detail and not cond else ""))
    if not cond:
        FAIL.append(name)


def raises(exc, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except exc:
        return True
    except Exception:
        return False
    return False


def abort_code(fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except rg.SafetyAbort as exc:
        return exc.code
    except Exception as exc:
        return type(exc).__name__
    return None


class Clock:
    def __init__(self, t=1000.0, step=0.01):
        self.t = float(t)
        self.step = float(step)

    def __call__(self):
        self.t += self.step
        return self.t

    def sleep(self, dt):
        self.t += float(dt)


def simulator(**kwargs):
    clock = kwargs.pop("clock", None) or Clock()
    transport = rg.SimulatedTransport(clock=clock, **kwargs)
    return clock, transport


def controller(transport, clock, config=None, vision=None, dry_run=True,
               people_clear=True, people_cleared_at=None, log_path=None):
    config = config or rg.RuleConfig(
        settle_poll_s=0.01,
        gripper_settle_s=0.01,
        settle_timeout_s=0.5,
        heartbeat_interval_s=5.0,
    )
    log = rg.EventLog(log_path, "test-run", clock)
    return rg.RuleGraspController(
        transport, config=config, log=log, clock=clock, sleep=clock.sleep,
        people_clear=people_clear,
        people_cleared_at=(clock() if people_cleared_at is None
                           else people_cleared_at),
        vision_probe=vision, vision_cam=4, dry_run=dry_run,
        run_id="test-run",
    )


def default_doc(delta=4.0, joint=0):
    return {"arm": 0, "approach": [{"joint": joint, "delta_deg": delta}]}


def run_case(transport, clock, doc=None, config=None, vision=None,
             dry_run=True, people_clear=True, people_cleared_at=None,
             log_path=None):
    ctl = controller(transport, clock, config=config, vision=vision,
                     dry_run=dry_run, people_clear=people_clear,
                     people_cleared_at=people_cleared_at, log_path=log_path)
    align, approach = ctl.plan(doc or default_doc())
    result = ctl.run(align, approach, gripper_arm=0)
    return ctl, result


def observation(x=100.0, y=100.0, width=40.0, detected=True, confidence=0.9,
                source="test"):
    return {"detected": detected, "center_x": x, "center_y": y,
            "width_px": width, "confidence": confidence, "source": source}


def sequence_probe(*values):
    it = iter(values)

    def probe(_frame):
        return next(it)

    return probe


# ---------------------------------------------------------------------------
# validation and planning
# ---------------------------------------------------------------------------


def test_validation():
    check("arm 0 accepted", rg.validate_arm(0) == 0)
    check("arm 1 accepted", rg.validate_arm(1) == 1)
    for value in (True, False, 1.0, "1", "right", 2, -1, None):
        check("arm %r rejected" % (value,), raises(rg.ValidationError,
                                                   rg.validate_arm, value))
    check("joint 0 accepted", rg.validate_joint(0) == 0)
    check("joint 5 accepted", rg.validate_joint(5) == 5)
    for value in (True, 6, -1, 1.0, "J1"):
        check("joint %r rejected" % (value,), raises(rg.ValidationError,
                                                     rg.validate_joint, value))
    check("delta 2.0 accepted", rg.validate_delta(2.0) == 2.0)
    check("delta -2.0 accepted", rg.validate_delta(-2.0) == -2.0)
    for value in (2.0001, -2.0001, float("nan"), float("inf"), "1", True):
        check("delta %r rejected" % (value,), raises(rg.ValidationError,
                                                     rg.validate_delta, value))
    check("ArmCommand keeps integer arm",
          isinstance(rg.ArmCommand(1, 3, 1.5).body()["arm"], int))
    check("ArmCommand rejects string arm",
          raises(rg.ValidationError, rg.ArmCommand, "right", 0, 1.0))
    check("ArmCommand rejects bool arm",
          raises(rg.ValidationError, rg.ArmCommand, True, 0, 1.0))


def test_split():
    pieces = rg.split_delta(5.0, 2.0)
    check("5 deg splits into 2+2+1", pieces == [2.0, 2.0, 1.0], pieces)
    check("split preserves the total", abs(sum(pieces) - 5.0) < 1e-9, pieces)
    neg = rg.split_delta(-4.9, 2.0)
    check("negative split preserves sign",
          neg == [-2.0, -2.0, -0.9], neg)
    check("small delta is one command", rg.split_delta(1.999, 2.0) == [1.999])
    check("zero delta is no command", rg.split_delta(0.0, 2.0) == [])
    check("split rejects a cap above the API limit",
          raises(rg.ValidationError, rg.split_delta, 3.0, 2.5))


def test_plan_move():
    plan = rg.plan_joint_move(1, 3, 10.0, 15.0,
                              rg.PlanBudget(max_total_deg_per_joint=20.0))
    check("move plan splits 5 deg into 3 commands", len(plan.commands) == 3,
          len(plan.commands))
    check("move plan stays on arm 1 joint 3",
          all(c.arm == 1 and c.joint == 3 for c in plan.commands))
    check("move plan never exceeds 2 deg",
          all(abs(c.delta_deg) <= 2.0 + 1e-9 for c in plan.commands))
    check("move plan totals 5 deg",
          abs(sum(c.delta_deg for c in plan.commands) - 5.0) < 1e-9)
    check("target outside the J1 box is refused",
          raises(rg.ValidationError, rg.plan_joint_move, 0, 0, 0.0, 151.0))
    check("J2 may move from -2 deg toward its box",
          len(rg.plan_joint_move(0, 1, -2.0, -1.0).commands) == 1)
    check("J2 may not move further outside its box",
          raises(rg.ValidationError, rg.plan_joint_move, 0, 1, -2.0, -3.0))
    check("single-move travel cap is enforced",
          raises(rg.ValidationError, rg.plan_joint_move, 0, 0, 0.0, 30.0,
                 rg.PlanBudget(max_total_deg_per_joint=10.0)))
    check("command-count cap is enforced",
          raises(rg.ValidationError, rg.plan_joint_move, 0, 0, 0.0, 5.0,
                 rg.PlanBudget(max_commands=2)))


def test_plan_sequence():
    start = {0: (0.0,) * 6}
    goals = [
        rg.JointGoal.delta(0, 0, 20.0),
        rg.JointGoal.delta(0, 1, 10.0),
        rg.JointGoal.delta(0, 2, -8.0),
        rg.JointGoal.delta(0, 3, 8.0),
    ]
    plan = rg.plan_joint_sequence(start, goals,
                                  rg.PlanBudget(max_total_deg_per_joint=30.0))
    joints = [c.joint for c in plan.commands]
    check("extension keeps the J1..J4 order",
          joints == sorted(joints), joints)
    check("extension ends at the requested pose",
          plan.end_pose_by_arm[0] == (20.0, 10.0, -8.0, 8.0, 0.0, 0.0),
          plan.end_pose_by_arm[0])
    check("extension counts J1 travel", plan.total_deg_by_joint[(0, 0)] == 20.0)
    check("extension uses <=2 deg steps",
          all(abs(c.delta_deg) <= 2.0 + 1e-9 for c in plan.commands))

    virtual = rg.plan_joint_sequence(
        {0: (10.0,) * 6},
        [rg.JointGoal.delta(0, 1, 5.0), rg.JointGoal.target(0, 1, 20.0)])
    check("a later target is evaluated after the earlier leg",
          virtual.end_pose_by_arm[0][1] == 20.0,
          virtual.end_pose_by_arm[0])
    check("cumulative per-joint travel is capped",
          raises(rg.ValidationError, rg.plan_joint_sequence,
                 {0: (0.0,) * 6},
                 [rg.JointGoal.delta(0, 0, 30.0),
                  rg.JointGoal.delta(0, 0, 20.0)],
                 rg.PlanBudget(max_total_deg_per_joint=45.0)))

    mixed = rg.plan_joint_sequence(
        {0: (0.0,) * 6, 1: (0.0,) * 6},
        [rg.JointGoal.delta(1, 4, 4.0), rg.JointGoal.delta(0, 3, 2.0)])
    check("mixed-arm sequence routes each command",
          [(c.arm, c.joint) for c in mixed.commands] ==
          [(1, 4), (1, 4), (0, 3)], mixed.commands)

    check("J-name parsing is 1-based",
          rg.parse_joint_ref("J1") == 0 and rg.parse_joint_ref("j6") == 5)
    check("J7 is refused", raises(rg.ValidationError, rg.parse_joint_ref, "J7"))
    extend = rg.parse_extend_spec("J1:+20,J4:-6", 1)
    check("extend spec builds ordered goals",
          [(g.arm, g.joint, g.delta_deg) for g in extend] ==
          [(1, 0, 20.0), (1, 3, -6.0)])


# ---------------------------------------------------------------------------
# state machine happy path
# ---------------------------------------------------------------------------


def test_dry_run_happy_path():
    clock, transport = simulator()
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "run.jsonl")
        ctl, result = run_case(transport, clock, log_path=path)
        check("dry run reaches VERIFY", result.state == "VERIFY", result.state)
        check("dry run does not claim success", result.success is False)
        check("dry run is marked", result.dry_run is True)
        check("dry run reports not verified",
              result.reason == "DRY_RUN_NOT_VERIFIED", result.reason)
        check("contact candidate found", result.contact is True)
        check("dry run sends arm commands", len(transport.arm_commands) == 2,
              len(transport.arm_commands))
        check("gripper is closed in segments",
              len(transport.gripper_commands) >= 2,
              len(transport.gripper_commands))
        check("gripper segments are bounded",
              all(abs(c.delta_deg) <= 5.0 for c in transport.gripper_commands))
        check("every arm command stayed <=2 deg",
              all(abs(c.delta_deg) <= 2.0 for c in transport.arm_commands))
        check("cleanup stops the chassis", transport.safe_stops == 1)
        check("cleanup stops recording", transport.record_stops == 1)
        check("cleanup releases the lease", transport.releases == 1)
        states = [e["new"] for e in result.events if e["event"] == "state"]
        check("state order is ALIGN/APPROACH/CLOSE/VERIFY",
              states == ["ALIGN", "APPROACH", "CLOSE", "VERIFY"], states)

        rows = [json.loads(line) for line in open(path, encoding="utf-8")]
        actions = [r for r in rows if r["event"] == "action"]
        check("JSONL records every action", len(actions) == result.commands,
              (len(actions), result.commands))
        check("every action has before/after state",
              all(r.get("before_arm") and r.get("after_arm") for r in actions))
        check("every action has before/after image refs",
              all(r.get("before_image", {}).get("sha256") and
                  r.get("after_image", {}).get("sha256") for r in actions))
        check("JSONL run_end never says success on a dry run",
              rows[-1]["event"] == "run_end" and rows[-1]["success"] is False,
              rows[-1])


def test_execute_positive_path():
    clock, transport = simulator()
    probe = sequence_probe(observation(y=100.0), observation(y=90.0))
    ctl, result = run_case(transport, clock, dry_run=False, vision=probe)
    check("execute path can verify a follow + lift",
          result.success is True and result.verified is True,
          (result.success, result.reason))
    check("verified execute run marks recording success",
          transport.record_success == [True], transport.record_success)
    check("contact candidate is required for success", result.contact is True)


# ---------------------------------------------------------------------------
# aborts
# ---------------------------------------------------------------------------


def _abort(transport, code, **kwargs):
    clock = transport.clock
    ctl, result = run_case(transport, clock, **kwargs)
    check("abort code is %s" % code, result.reason == code, result.reason)
    check("%s run is not success" % code, result.success is False)
    check("%s still cleans up" % code,
          transport.safe_stops == 1 and transport.record_stops == 1 and
          transport.releases == 1,
          (transport.safe_stops, transport.record_stops, transport.releases))
    return ctl, result


def test_aborts():
    clock, transport = simulator()
    transport.faults[0] = [("driver_error", "motor fault")]
    _abort(transport, "FAULT")

    clock, transport = simulator(fault_after_commands=1)
    ctl, result = _abort(transport, "FAULT")
    check("mid-run fault stopped before the whole plan",
          len(transport.arm_commands) < 4, len(transport.arm_commands))

    clock, transport = simulator()
    transport.feedback_age = [9.0, 9.0]
    _abort(transport, "FEEDBACK_STALE")

    clock, transport = simulator(stale_after_commands=1)
    _abort(transport, "FEEDBACK_STALE")

    clock, transport = simulator()
    transport.targets[0][0] = 20.0
    ctl = controller(transport, clock)
    check("a large start lag refuses planning",
          abort_code(ctl.plan, default_doc()) == "TARGET_NOT_ALIGNED")

    clock, transport = simulator()
    transport.targets[0][0] = 3.0
    ctl = controller(transport, clock)
    check("a 3 deg start lag refuses planning",
          abort_code(ctl.plan, default_doc()) == "TARGET_NOT_ALIGNED")

    clock, transport = simulator(lag_after_commands=1)
    _abort(transport, "TARGET_LAG")

    clock, transport = simulator()
    transport.lease_ok = False
    _abort(transport, "LEASE_LOST")

    clock, transport = simulator(lease_lost_after_commands=1)
    _abort(transport, "LEASE_LOST")

    clock, transport = simulator()
    _abort(transport, "PEOPLE_NOT_CLEAR", people_clear=False)

    clock, transport = simulator()
    ctl, result = run_case(transport, clock, people_clear=True,
                           people_cleared_at=clock.t - 1000.0)
    check("expired clearance aborts", result.reason == "PEOPLE_CLEAR_EXPIRED",
          result.reason)

    clock, transport = simulator(estop=True)
    _abort(transport, "ESTOP")

    clock, transport = simulator(image_age_s=10.0)
    _abort(transport, "IMAGE_STALE")

    class NoAge(rg.SimulatedTransport):
        def status(self):
            out = super().status()
            out["arms"][0]["feedback_age_s"] = None
            return out

    clock = Clock()
    transport = NoAge(clock=clock)
    _abort(transport, "FEEDBACK_STALE")

    class NotFeedbackOk(rg.SimulatedTransport):
        def status(self):
            out = super().status()
            out["arms"][0]["feedback_ok"] = False
            return out

    clock = Clock()
    transport = NotFeedbackOk(clock=clock)
    _abort(transport, "FEEDBACK_LOST")

    class NotEnabledAll(rg.SimulatedTransport):
        def status(self):
            out = super().status()
            out["arms"][0]["enabled_all"] = False
            return out

    clock = Clock()
    transport = NotEnabledAll(clock=clock)
    _abort(transport, "DISABLED")

    class WrongSide(rg.SimulatedTransport):
        def status(self):
            out = super().status()
            out["arms"][0]["side"] = "right"
            return out

    clock = Clock()
    transport = WrongSide(clock=clock)
    ctl = controller(transport, clock)
    check("a misrouted arm is refused before motion",
          abort_code(ctl.plan, default_doc()) == "ARM_ROUTE")

    clock, transport = simulator(reject_after_commands=0)
    _abort(transport, "COMMAND_REJECTED")

    clock, transport = simulator(clamp_joint=0)
    _abort(transport, "TARGET_CLAMPED")

    clock, transport = simulator(measured_stuck=True)
    ctl, result = _abort(transport, "STALLED")
    sent = [e for e in result.events if e["event"] == "command_sent"]
    check("a command that stalls is still in the audit log", bool(sent), sent)
    check("a stalled command has a pre-state and image ref",
          sent and sent[0].get("before_arm") and
          sent[0].get("before_image", {}).get("sha256"))

    class LeaseDropsOnCommand(rg.SimulatedTransport):
        def arm(self, command):
            self.lease_ok = False
            raise RuntimeError("HTTP 409: lease expired")

    clock = Clock()
    transport = LeaseDropsOnCommand(clock=clock)
    _abort(transport, "LEASE_LOST")

    class FutureFeedback(rg.SimulatedTransport):
        def status(self):
            out = super().status()
            out["arms"][0]["feedback_age_s"] = -9.0
            return out

    clock = Clock()
    transport = FutureFeedback(clock=clock)
    _abort(transport, "FEEDBACK_STALE")

    class CleanupBroken(rg.SimulatedTransport):
        def safe_stop(self):
            super().safe_stop()
            return False

    clock = Clock()
    transport = CleanupBroken(clock=clock)
    ctl, result = run_case(transport, clock)
    cleanup = [e for e in result.events if e["event"] == "cleanup"]
    check("a failed zero-speed stop is logged as a cleanup error",
          cleanup and cleanup[-1]["ok"] is False, cleanup[-1:])

    clock, transport = simulator()
    cfg = rg.RuleConfig(settle_poll_s=0.01, settle_timeout_s=0.5,
                        max_plan_duration_s=0.001)
    _abort(transport, "PLAN_TIMEOUT", config=cfg)

    # Plan with a generous budget, execute with a tighter travel cap.
    clock, transport = simulator()
    ctl = controller(transport, clock, config=rg.RuleConfig(
        settle_poll_s=0.01, settle_timeout_s=0.5,
        max_total_deg_per_joint=3.0))
    align, approach = ctl.plan(default_doc(4.0),
                               budget=rg.PlanBudget(max_total_deg_per_joint=10.0))
    result = ctl.run(align, approach)
    check("cumulative travel cap aborts at execution",
          result.reason == "TRAVEL_LIMIT", result.reason)

    clock, transport = simulator()
    ctl = controller(transport, clock, config=rg.RuleConfig(
        settle_poll_s=0.01, settle_timeout_s=0.5, max_commands=1))
    align, approach = ctl.plan(default_doc(4.0),
                               budget=rg.PlanBudget(max_commands=10))
    result = ctl.run(align, approach)
    check("the run-wide command cap aborts",
          result.reason == "COMMAND_LIMIT", result.reason)

    check("a heartbeat slower than the lease TTL is refused",
          raises(rg.ValidationError, rg.RuleConfig, heartbeat_interval_s=11.0))
    check("feedback older than the server stale window is refused",
          raises(rg.ValidationError, rg.RuleConfig, max_feedback_age_s=2.5))

    clock, transport = simulator()
    ctl = controller(transport, clock)
    check("illegal state transition is refused",
          abort_code(ctl._transition, rg.State.CLOSE) == "BAD_TRANSITION")


# ---------------------------------------------------------------------------
# transport / lease / cleanup
# ---------------------------------------------------------------------------


class FakeLease:
    def __init__(self):
        self.secret = "secret"
        self.lost = False
        self.calls = []
        self.acquired = False
        self.released = False
        self.heartbeats = 0

    def acquire(self, takeover=False, confirm=None):
        self.acquired = True
        return True

    def get(self, path, timeout=None):
        self.calls.append(("GET", path, None))
        return {"ok": True, "arms": []}

    def post(self, path, obj, timeout=None):
        self.calls.append(("POST", path, dict(obj or {})))
        return {"ok": True}

    def heartbeat(self):
        self.heartbeats += 1

    def release(self):
        self.released = True


def test_http_transport():
    lease = FakeLease()
    transport = rg.HttpTransport("http://127.0.0.1:9", "test", lease=lease)
    response = transport.arm(rg.ArmCommand(1, 3, 1.5))
    method, path, body = lease.calls[-1]
    check("arm routes to /api/arm", method == "POST" and path == "/api/arm")
    check("arm body uses integer arm=1", body.get("arm") == 1 and
          isinstance(body.get("arm"), int), body)
    check("arm body keeps joint and delta",
          body.get("joint") == 3 and body.get("delta_deg") == 1.5, body)
    check("transport returns the server response", response.get("ok") is True)

    transport.gripper(rg.GripperCommand(0, -5.0))
    check("gripper body is relative and integer-routed",
          lease.calls[-1][1] == "/api/gripper" and
          lease.calls[-1][2] == {"arm": 0, "delta_deg": -5.0},
          lease.calls[-1])

    check("fresh lease is valid", transport.lease_valid() is True)
    lease.lost = True
    check("lost lease is invalid", transport.lease_valid() is False)

    transport.safe_stop()
    check("safe stop sends a zero drive",
          lease.calls[-1][1] == "/api/drive" and
          lease.calls[-1][2] == {"lin": 0.0, "ang": 0.0}, lease.calls[-1])
    transport.stop_recording(False)
    check("recording stop marks failure false",
          lease.calls[-1][1] == "/api/record/stop" and
          lease.calls[-1][2] == {"success": False}, lease.calls[-1])
    transport.stop_recording(True)
    check("recording stop marks verified success true",
          lease.calls[-1][2] == {"success": True}, lease.calls[-1])
    transport.release()
    check("release reaches the lease client", lease.released is True)

    cls = rg._load_lease_session_class()
    check("LeaseSession is reused from run-smolvla.py",
          cls.__name__ == "LeaseSession")


def test_heartbeat():
    clock, transport = simulator()
    ctl = controller(transport, clock, dry_run=False,
                     config=rg.RuleConfig(settle_poll_s=0.01,
                                          heartbeat_interval_s=0.01))
    ctl._last_heartbeat = clock.t - 10.0
    ctl._maybe_heartbeat(clock())
    check("a due heartbeat is sent", transport.heartbeats == 1,
          transport.heartbeats)

    class Broken(rg.SimulatedTransport):
        def heartbeat(self):
            raise RuntimeError("socket closed")

    clock = Clock()
    transport = Broken(clock=clock)
    ctl = controller(transport, clock, dry_run=False,
                     config=rg.RuleConfig(settle_poll_s=0.01,
                                          heartbeat_interval_s=0.01))
    ctl._last_heartbeat = clock.t - 10.0
    check("a failed heartbeat aborts as LEASE_LOST",
          abort_code(ctl._maybe_heartbeat, clock()) == "LEASE_LOST")


# ---------------------------------------------------------------------------
# false-success protection
# ---------------------------------------------------------------------------


def test_verification():
    good_base = rg.BoardObservation(True, 100, 100, 40, 0.9, 1000.0)
    good_after = rg.BoardObservation(True, 100, 90, 40, 0.9, 1001.0)
    cfg = rg.VerificationConfig(min_follow_px=8, min_lift_px=4)

    ok = rg.verify_grasp(good_base, good_after, True, 1001.0, 1001.1, 2.0, cfg)
    check("follow + lift + contact verifies", ok.ok and ok.reason == "VERIFIED",
          ok)

    no_contact = rg.verify_grasp(good_base, good_after, False, 1001.0,
                                 1001.1, 2.0, cfg)
    check("HTTP 200 without contact is not success",
          not no_contact.ok and no_contact.reason == "NO_CONTACT")

    stale = rg.verify_grasp(good_base, good_after, True, 900.0, 1001.1,
                            2.0, cfg)
    check("stale image is not success",
          not stale.ok and stale.reason == "IMAGE_STALE")

    invisible = rg.verify_grasp(good_base,
                                rg.BoardObservation(False, 0, 0, 0, 0.0, 1001.0),
                                True, 1001.0, 1001.1, 2.0, cfg)
    check("invisible board is not success",
          not invisible.ok and invisible.reason == "BOARD_NOT_VISIBLE")

    low = rg.verify_grasp(good_base,
                          rg.BoardObservation(True, 100, 90, 40, 0.1, 1001.0),
                          True, 1001.0, 1001.1, 2.0, cfg)
    check("low confidence is not success",
          not low.ok and low.reason == "LOW_CONFIDENCE")

    still = rg.verify_grasp(good_base, good_base, True, 1001.0, 1001.1,
                            2.0, cfg)
    check("unmoving board is not success",
          not still.ok and still.reason == "BOARD_NOT_FOLLOWING")

    no_lift = rg.verify_grasp(good_base,
                              rg.BoardObservation(True, 110, 100, 40, 0.9, 1001.0),
                              True, 1001.0, 1001.1, 2.0, cfg)
    check("sideways motion without lift is not success",
          not no_lift.ok and no_lift.reason == "NO_LIFT")

    scaled = rg.verify_grasp(good_base,
                             rg.BoardObservation(True, 100, 90, 80, 0.9, 1001.0),
                             True, 1001.0, 1001.1, 2.0, cfg)
    check("a width jump is not success",
          not scaled.ok and scaled.reason == "SCALE_JUMP")

    stale_obs = rg.verify_grasp(good_base,
                                rg.BoardObservation(True, 100, 90, 40, 0.9, 900.0),
                                True, 1001.0, 1001.1, 2.0, cfg)
    check("a stale observation timestamp is not success",
          not stale_obs.ok and stale_obs.reason == "IMAGE_STALE")

    no_obs = rg.verify_grasp(None, good_after, True, 1001.0, 1001.1, 2.0, cfg)
    check("missing baseline is not success",
          not no_obs.ok and no_obs.reason == "NO_OBSERVATION")


def test_false_success_controller():
    # Execute with contact but the board does not move.
    clock, transport = simulator()
    probe = sequence_probe(observation(y=100.0), observation(y=100.0))
    ctl, result = run_case(transport, clock, dry_run=False, vision=probe)
    check("execute does not trust contact alone",
          result.success is False and result.reason == "VERIFY_FAILED",
          (result.success, result.reason))
    check("failed execute records success=false",
          transport.record_success == [False], transport.record_success)

    # An advisory VLM "success" cannot rescue a failed verification.
    clock, transport = simulator()
    probe = sequence_probe(observation(y=100.0), observation(y=100.0))
    ctl = controller(transport, clock, dry_run=False, vision=probe)
    ctl.add_advisory(rg.Advisory("vlm", {"grasp": True}, 0.99,
                                 "looks grasped"))
    align, approach = ctl.plan(default_doc())
    result = ctl.run(align, approach)
    check("VLM advisory cannot turn a failure into success",
          result.success is False and result.reason == "VERIFY_FAILED",
          (result.success, result.reason))

    # Dry run with a perfect probe is still not a claimed success.
    clock, transport = simulator()
    probe = sequence_probe(observation(y=100.0), observation(y=90.0))
    ctl, result = run_case(transport, clock, dry_run=True, vision=probe)
    check("dry run never claims success even with a perfect probe",
          result.success is False and result.reason == "DRY_RUN_NOT_VERIFIED",
          (result.success, result.reason))

    # Contact never happens: closing to zero is not success.
    clock, transport = simulator(contact_at_deg=-10.0)
    probe = sequence_probe(observation(y=100.0), observation(y=90.0))
    ctl, result = run_case(transport, clock, dry_run=False, vision=probe)
    check("closing without contact is not success",
          result.success is False and result.reason == "VERIFY_FAILED" and
          result.contact is False, (result.success, result.reason,
                                    result.contact))

    # A no-detection probe is not success.
    clock, transport = simulator()
    probe = sequence_probe(observation(), observation(detected=False))
    ctl, result = run_case(transport, clock, dry_run=False, vision=probe)
    check("a missed detection is not success",
          result.success is False and result.reason == "VERIFY_FAILED",
          (result.success, result.reason))


# ---------------------------------------------------------------------------
# CLI guards
# ---------------------------------------------------------------------------


def test_cli_guards():
    check("execute without confirm is refused",
          rg.main(["--demo", "--execute", "--people-clear",
                   "--vision", "x:y"]) == 2)
    check("execute without people-clear is refused",
          rg.main(["--demo", "--execute", "--confirm", "EXECUTE",
                   "--vision", "x:y"]) == 2)
    check("execute without vision is refused",
          rg.main(["--demo", "--execute", "--confirm", "EXECUTE",
                   "--people-clear"]) == 2)
    check("bad arm is refused by argparse",
          raises(SystemExit, rg.main, ["--demo", "--arm", "2"]))
    check("start-pose is dry-run only",
          rg.main(["--demo", "--execute", "--confirm", "EXECUTE",
                   "--people-clear", "--vision", "x:y",
                   "--start-pose", "0,0,0,0,0,0"]) == 2)


def main():
    test_validation()
    test_split()
    test_plan_move()
    test_plan_sequence()
    test_dry_run_happy_path()
    test_execute_positive_path()
    test_aborts()
    test_http_transport()
    test_heartbeat()
    test_verification()
    test_false_success_controller()
    test_cli_guards()

    print("\n%d checks, %d failed" % (COUNT[0], len(FAIL)))
    if FAIL:
        for name in FAIL:
            print("  FAILED: " + name)
        return 1
    print("ALL RULE GRASP TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
