# Incident: arm shaking, 2026-10-29

Short version: **a mode switch did not take effect, and the controller kept
streaming position targets into an arm that was planning its own trajectory.**
It was a bug in this controller, not a hardware fault. The operator cut arm power
to stop it, which is what made both CAN buses go silent.

## Timeline (all times local, robot clock)

| Time | Event |
|---|---|
| 15:19 | Service deployed with the new operating modes. Arms at startup pose. |
| 15:21 | On-hardware smoke test: `pos_p` round trip, trajectory replay (points = measured pose), master/slave OFF. Reported 0.00° drift. |
| ~15:21 | `pos_j` was requested to switch back. **The arm did not accept it.** |
| 15:21–15:27 | Controller streamed `JointCtrl` at 50 Hz into an arm in `MOVE_P`. |
| ~15:27 | Operator observed the arms shaking. |
| 15:27:44 | Operator cut arm power. First `SendCanMessage(SEND_MESSAGE_FAILED)` in the log. |
| 15:28 | Both buses: 0 frames/s, `ERROR-PASSIVE`, joints read 0.0. |

## Evidence

At 15:29 the arm's own status frame (cached from the last moment before power
loss) reported:

```
Control Mode: STANDBY(0x0)
Mode Feed:    MOVE_P(0x0)        <- the arm was in MOVE P
```

while the controller's `/api/status` said `mode: pos_j`. The controller was
tracking its own bookkeeping, never the hardware.

Two contributing facts:

* **MOVE P plans its own path.** A 50 Hz position stream makes the planner fight
  the stream. That is the shaking.
* **The "resync to measured" step hard-clamped to the datasheet limits.** The
  left arm legitimately rests at J2 = −2.06° and J3 = +1.44° against limits of
  `0..180` and `−170..0`, so every resync invented a permanent ~2° target error
  that the arm pushed against (audible buzz, visible jitter).

## Fixes

1. **`set_mode` verifies against the hardware.** After sending `MotionCtrl_2` it
   reads `GetArmStatus()`, parses `Mode Feed:`, retries once, and if the arm still
   disagrees it **reverts its own bookkeeping to what the arm is actually in** and
   returns failure. `/api/status` now exposes `mode_actual` and `mode_verified`.
2. **`pos_p` is one-shot.** The loop sends the target only when it changes, never
   as a stream.
3. **Resync no longer hard-clamps.** `_resync_target_locked()` adopts the measured
   pose with a `RESYNC_MARGIN_DEG` (default 5°) tolerance, so "hold where you are"
   really holds where you are, while still rejecting absurd feedback.
4. **Startup refuses a silent bus.** If `/sys/class/net/<iface>/statistics/rx_packets`
   does not advance over 2 s, the arm is marked `NOT ok` and **no target or home
   pose is adopted** — previously a dead bus was read as `joints=[0,0,0,0,0,0]` and
   that zero pose became `home`, so a later "Home" would have commanded an
   arbitrary all-zero pose.

## Verification

* `tools/test-modes-offline.py` — 36 → **46 assertions**, including two regression
  tests: "an arm that refuses the mode change must not be faked" and "resync keeps
  a measured pose just outside the limits but still rejects absurd feedback".
* `scripts/verify-modes.sh` on the real robot — 12/12 pass. The arm now reports
  `MOVE_P` when asked for `pos_p` and `MOVE_J` when asked back, confirmed by
  hardware read-back; the arm moved 0.17° during the whole round trip.

## If it happens again

1. Do not cut power as the first move — that leaves the arms limp and the cause
   unrecorded. Read `/api/status` first; `mode_actual` now tells you what the arm
   believes.
2. `POST /api/arm/reset` re-syncs the target to the measured pose and re-enables;
   it causes no motion.
3. Check `/api/status` → `jitter.cmd_hz`; it should sit near 50.
