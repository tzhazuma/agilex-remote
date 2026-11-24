# feedback_control.py

Offline feedback decoding and bounded setpoint control for the Tracer base and
the two Piper arms. Stdlib only; it never opens a CAN bus, socket, camera or
robot SDK. `tools/test-feedback-control.py` replays real captured bytes, so the
decoders are checked without a robot.

## Files

| File | Purpose |
|---|---|
| `feedback_control.py` | frame decoders, calibration, quality gate, chassis and arm loops |
| `test-feedback-control.py` | 380+ stdlib checks, including the 2026-11-24 real frames |

## Frame formats and buses

| CAN ID | Bus | Payload | Decoder |
|---|---|---|---|
| `0x311` | `can0` | `int32 left_wheel, int32 right_wheel` (big-endian, cumulative counts) | `parse_wheel_counts` |
| `0x251` / `0x252` | `can0` | `int16 rpm, int16 current*0.1 A, int32 pulse_count` | `parse_actuator_hs` |
| `0x2A1` | `can_left` / `can_right` | `u8 ctrl_mode, u8 arm_status, u8 mode_feed, u8 teach_status, u8 motion_status, u8 trajectory_num, u16 err_code` | `parse_arm_status` |
| `0x2A5` / `0x2A6` / `0x2A7` | `can_left` / `can_right` | two `int32` joint angles, unit 0.001 deg, big-endian | `parse_arm_joints` |

`decode_frame(can_id, data, timestamp, iface, clock)` requires an explicit
interface for every supported ID; a missing or wrong bus raises
`FrameBusError`. `0x251`/`0x252` on an arm bus are Piper high-speed frames with
a different payload and are rejected, not misread as Tracer actuators.
`0x313`, `0x221`, `0x211` and every other ID are rejected with
`UnsupportedFrame` until their meaning is verified on this chassis.

`parse_candump_line()` preserves the interface and marks the clock as `epoch`
when a timestamp is present. A live reader should pass `iface=...` and
`clock="monotonic"` explicitly. The module never mixes clock domains: a sample
whose clock is unknown or differs from the configured one is rejected.

## Chassis

```python
frame = parse_candump_line("(1795519939.614529) can0 311#0000022800000296")
sample = decode_frame(frame.can_id, frame.data, frame.timestamp,
                      iface=frame.iface, clock=frame.clock)

cal = WheelCalibration(counts_per_meter=1000.0, track_width_m=0.4)
cfg = ChassisControlConfig(clock="epoch",
                           gate=FeedbackGateConfig(clock="epoch"))
ctl = ChassisVelocityController(cfg, cal)
cmd = ctl.step(frame.timestamp, target_linear_mps=0.05,
               target_angular_rps=0.0, sample=sample)
```

The first sample is `WARMUP` (zero command) because a rate needs two samples.
After that the loop commands `MOVING`/`HOLD` only while the gate passes.

`ChassisFeedbackGate` rejects:

| Code | Meaning |
|---|---|
| `NO_FEEDBACK` / `STALE` / `FUTURE` | missing, older than `max_age_s`, future timestamp, or `dt > max_dt_s` |
| `CLOCK_UNKNOWN` / `CLOCK_MISMATCH` | sample has no clock domain, or it differs from the configured clock |
| `UNCALIBRATED` | no calibration, source mismatch, or pulse mapping mismatch |
| `ZERO_BUT_COMMANDED` | counts are constant while a nonzero linear/angular motion is commanded |
| `JUMP` | an unwrapped wheel delta exceeds `max_count_jump` |
| `OUT_OF_RANGE` | converted linear/angular speed exceeds the configured cap |

Any rejection, emergency stop or warmup returns `linear_mps=0.0`,
`angular_rps=0.0`, `ttl_s=0.0` and the rejection code. There is no fallback to
an unvalidated source. `FUTURE`, `JUMP`, `CLOCK_UNKNOWN` and `CLOCK_MISMATCH`
do not update the baseline, so a bad frame cannot poison later decisions;
`STALE` (including a long `dt`) updates the baseline and recovers on the next
valid frame.

### Counts to metres

`wheel_motion(previous, current, calibration, max_dt_s=None)` is the only
conversion path. Without a `WheelCalibration` it returns `valid=False`,
`code="UNCALIBRATED"` and `distance_m=None` / `yaw_rad=None`; it never invents
a scale or wheel base. `counts_per_meter`, `track_width_m`, and the per-wheel
signs are explicit. Int32 counters are unwrapped at 2**31, so a rollover is a
small delta rather than a permanent `JUMP`.

`source` binds a calibration to either `wheel_0x311` or `actuator_pulse`.
Actuator pulse calibration additionally needs a `PulseMapping`; the module
does not assume that `0x251` is the left wheel.

## Arm

```python
asm = ArmFeedbackAssembler()
for can_id, data, ts in frames:          # 0x2A5, 0x2A6, 0x2A7, 0x2A1
    asm.feed(decode_frame(can_id, data, ts,
                          iface=iface, clock="monotonic"))

# The adapter must supply the real motor-enable state and either the lag or
# the current bounded setpoint; the assembler never invents True/0.0.
feedback = asm.snapshot(enabled=arm_enabled,
                        target_joints_deg=current_setpoint)
ctl = JointSetpointController(target_joints_deg, ArmControlConfig())
cmd = ctl.step(now, feedback)
```

`target_joints_deg` here means the **current commanded setpoint** (for example
the previous small step), not a distant final goal; otherwise its target lag
will correctly exceed `max_lag_deg` and abort with `LAG`.

- `snapshot()` returns `None` until all six joints, the `0x2A1` status, one
  common bus and one common clock are present; a stale status frame cannot be
  hidden by fresh joint frames (the oldest timestamp is used).
- `enabled=None` and `lag_deg=None` are rejected by the controller as
  `DISABLED` / `LAG`. A missing enable state is not treated as enabled; a
  missing lag is not treated as zero.
- every joint step is at most `max_step_deg` (default 0.5 deg) and never goes
  past the target;
- success needs `settle_steps` consecutive samples inside `tolerance_deg`;
- abort statuses: `FEEDBACK_LOST`, `FAULT`, `DISABLED`, `MODE`, `LAG`,
  `CLOCK_UNKNOWN`, `CLOCK_MISMATCH`, `UNREACHABLE`, `STALLED`, `TIMEOUT`,
  `MAX_ITERATIONS`, `OVERSHOOT`, `SAFETY`, `ESTOP`;
- `MODE` covers both `ctrl_mode != 1 (CAN)` and `mode_feed` outside
  `MOVE_J`/`MOVE_P`;
- `motion_status=1` means "not yet at target" in the SDK and is informational,
  not a fault; faults come from `arm_status` and `err_code`;
- the first `FEEDBACK_LOST` with no history returns an empty target tuple
  (nothing to hold); after a successful step it holds the last measured pose,
  never the goal target;
- a measured joint already slightly outside its nominal limit may be moved
  back inside within `limit_margin_deg` (default 5 deg). The controller never
  commands a joint further outside: for an under-limit joint the command may
  not go below the measured value, and for an over-limit joint it may not go
  above it. Beyond the margin it stops with `SAFETY`.

## Calibration (robot-side, not performed here)

This procedure requires the robot to be powered, the area clear, and a person
at the e-stop. It was **not** executed in this workspace.

1. **Wheel scale (`0x311`).** Mark a straight 1.0 m line. Record `0x311` before
   and after a slow (<=0.05 m/s) straight run. `counts_per_meter =
   (|d_left| + |d_right|) / 2 / 1.0`. Confirm both deltas have the **same
   sign**; if one counts down while driving forward, set that side's
   `left_sign`/`right_sign` to `-1`. Resting cumulative values or their signs
   are **not** a direction calibration.
2. **Track width.** Rotate in place through 90 deg (pi/2). With the calibrated
   scale, `track_width_m = (right_m - left_m) / (pi/2)`. Repeat in the other
   direction and use the average; a tape-measure wheel-base is a useful sanity
   check, not the final value.
3. **Motor pulse fallback (`0x251`/`0x252`).** First determine which CAN ID is
   left and which is right by a slow forward test (do not use resting values:
   the audit's `-204503`/`+170784` are cumulative offsets, not directions).
   Record `PulseMapping(left_can_id=..., right_can_id=...)`. Then repeat step 1
   with `source="actuator_pulse"`, the same mapping, its own
   `counts_per_meter`, and signs chosen so both wheel deltas are positive when
   driving forward. Never reuse the `0x311` scale or mapping for pulses.
4. **Acceptance.** Drive 1.0 m at 0.05 and 0.08 m/s; distance error should stay
   within a few percent and straight-line yaw should stay near zero. Rotate
   90 deg both ways and check yaw error. Record the values in the deployment
   config, not in code.

Until steps 1-2 are done, the gate reports `UNCALIBRATED` and the controller
commands zero. That is intentional.

## Integration notes

- The existing server and browser client are unchanged; this module is a
  library under `tools/`. A production adapter should decode frames in its
  read-only CAN thread and call `ChassisVelocityController` / the arm
  controller at the server's existing command rate.
- Keep one clock domain. `candump -L` timestamps are epoch seconds; a live
  reader should pass `time.monotonic()` for both the frame and `now`. The
  controller's `clock` and `gate.clock` must match; mismatches are rejected
  with `CLOCK_*`. For candump replay use `ArmControlConfig(clock="epoch")`
  (or `expected_clock="epoch"` on `snapshot()`) instead of the default
  monotonic clock.
- Do not treat `0x251/0x252.rpm` or `/odom` as a validated source here.
  `0x221` was never observed nonzero while driving in the audit; `0x313` is
  undefined for this SDK; `/odom` publish is commented out on the robot.
- The arm path is an outer loop; the Piper firmware still owns the inner
  position servo. Keep the server's existing enable/mode checks.

## Tests

```bash
cd 02-代码
python -B tools/test-feedback-control.py
```

Expected: `ALL FEEDBACK CONTROL TESTS PASSED` (380+ checks), no CAN or network
access.
