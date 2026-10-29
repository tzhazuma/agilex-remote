# Piper arm operating modes in the remote controller

Everything below is implemented in `remote/robot-files/agilex-remote-server.py` and
exposed in `remote/robot-files/remote-ui.html`. Frame semantics were taken from the
installed `piper_sdk` on the robot (`C_PiperInterface_V3`), not from memory.

## Why modes at all

Until this round the controller had exactly one way to move an arm: hold a joint-space
target and stream `JointCtrl` at 50 Hz in `MOVE J`. That is the right default — it is
repeatable, it is what the episode recorder captures, and it is what an imitation-learning
policy expects to emit. But it is also the *only* thing the robot could do, which ruled out:

* compliant motion and hand guidance (needed for cheap demonstrations),
* Cartesian arcs (needed for anything that is naturally a curve),
* replaying a trajectory captured elsewhere,
* using the arm as a master/slave pair.

## The six things the controller can now do

| # | Mode / feature | SDK call | CAN | What it is for |
|---|---|---|---|---|
| 1 | `pos_j` — MOVE J | `MotionCtrl_2(move_mode=0x01)` + `JointCtrl` | 0x151 + 0x155 | Default. Joint-space position. Policy action space. |
| 2 | `pos_p` — MOVE P | `MotionCtrl_2(move_mode=0x00)` + `JointCtrl` | 0x151 + 0x155 | Arm plans the path to a point; smoother for big single moves. |
| 3 | `mit` — MIT impedance | `MotionCtrl_2(is_mit_mode=0xAD)` + `JointMitCtrl` | 0x151 + 0x15A–0x15F | Compliance: per-motor `kp`/`kd`/`t_ref`. |
| 4 | `movec` — MOVE C | `MotionCtrl_2(move_mode=0x03)` + `EndPoseCtrl` + `MoveCAxisUpdateCtrl` | 0x151 + 0x156/0x15C | Cartesian arc through start → via → end. |
| 5 | Trajectory stream | targets fed from a loaded list at `dt` | 0x155 | Replay a recorded or downloaded joint trajectory. |
| 6 | Drag teach | `MotionCtrl_1(grag_teach_ctrl=0x01/0x02/0x03)` | 0x150 | Record by hand, then replay. |
| 7 | Master/slave | `MasterSlaveConfig(linkage_config=0xFA/0xFC)` | 0x470 | Left arm hand-guided, right arm mirrors it. |

Verified signatures on the robot:

```
EndPoseCtrl(self, X: int, Y: int, Z: int, RX: int, RY: int, RZ: int)
MoveCAxisUpdateCtrl(self, instruction_num: Literal[0, 1, 2, 3] = 0)
JointMitCtrl(self, motor_num: int, pos_ref: float, vel_ref: float,
             kp: float, kd: float, t_ref: float)
MasterSlaveConfig(self, linkage_config: int, feedback_offset: int,
                  ctrl_offset: int, linkage_offset: int)
MotionCtrl_1(self, emergency_stop=0, track_ctrl=0, grag_teach_ctrl=0)
MotionCtrl_2(self, ctrl_mode=1, move_mode=1, move_spd_rate_ctrl=50,
             is_mit_mode=0, residence_time=0, installation_pos=0)
```

## Safety model

Four invariants were kept, because each one corresponds to a failure that was actually
observed on this robot:

1. **Target resync on every state change.** Switching mode, enabling, stopping a
   trajectory and starting teach all set the held target equal to the measured angle
   first. A disabled Piper goes limp, so its measured pose drifts away from the stale
   target; re-asserting that target is what made the arm lurch when it was switched back
   on.
2. **Lag guard applies to trajectories too.** A loaded trajectory is clamped to
   `MAX_TARGET_LAG_DEG` (default 15°) away from measured, per joint, exactly like a manual
   nudge. A trajectory captured on a different arm cannot yank this one across the room.
3. **Teaching and master/slave silence the command stream.** In `mit` the loop must send
   `JointMitCtrl` (a `JointCtrl` stream would be ignored); in `teaching` or while linked it
   must send *nothing*, because any frame fights the operator's hand or the link.
4. **`set_enabled` re-asserts the ACTIVE mode.** It used to hard-code `MOVE J`, which
   silently dropped an arm out of MIT or MOVE C the moment it was re-enabled.

## API

All POST, JSON, `arm` is `0` = left, `1` = right.

| Endpoint | Body | Effect |
|---|---|---|
| `/api/arm/mode` | `{arm, mode}` | `pos_j` / `pos_p` / `mit` / `movec` |
| `/api/arm/mit` | `{arm, kp, kd, t_ref}` | scalars or 6-element lists |
| `/api/arm/movec` | `{arm, points}` | exactly 3 poses, integer SDK units |
| `/api/traj/load` | `{arm, name, points, dt}` | `points` = list of 6-angle poses |
| `/api/traj/play` | `{arm, loop, speed}` | 0.1–4.0× |
| `/api/traj/pause` / `/api/traj/stop` | `{arm}` | stop resyncs the target |
| `/api/teach` | `{arm, on, execute}` | on = record, execute = replay |
| `/api/master-slave` | `{on, leader, follower}` | link / unlink both arms |

`/api/status` now also returns, per arm: `mode`, `teaching`, `mit`, `movec_active`,
`traj {name, points, dt, idx, playing, loop, speed, plays}`, `master_slave`.

## What was verified, and what was not

`tools/test-modes-offline.py` — **36/36 assertions pass**. It injects a fake
`piper_sdk` and asserts the exact frame arguments for every mode: `pos_p` sends
`move_mode=0x00` and `is_mit_mode=0`, `mit` sends `0xAD`, `movec` sends `move_mode=0x03`
and the 0/1/2 + execute sequence, teach sends `0x01/0x02/0x03`, leader sends `0xFA`,
follower `0xFC`, disable sends `0x00`. It also checks the trajectory runaway clamp.

`remote/mode-smoke.sh` — **23/23 checks pass on the real robot**. It exercises only the
paths that cannot move an arm: status shape, a `pos_p` → `pos_j` round trip (same target,
measured drift 0.00°), a trajectory whose three points equal the measured pose, and
master/slave OFF. Left arm moved 0.00° end to end, right arm 0.00°.

Deliberately **not** exercised on hardware yet, because each one makes a Piper compliant
and therefore hands it to gravity:

* MIT impedance on a live arm,
* drag-teach recording on a live arm,
* master/slave ON with both arms powered,
* MOVE C arcs.

Those four need a supervised first run in an empty workspace. The frames are correct; the
gain and pose values are not yet tuned.

## Using it

```bash
# drag-teach a demonstration, then replay it
curl -X POST :8765/api/teach -d '{"arm":0,"on":true}'
#   ...move the left arm by hand...
curl -X POST :8765/api/teach -d '{"arm":0,"on":false}'
curl -X POST :8765/api/teach -d '{"arm":0,"on":false,"execute":true}'

# replay a joint trajectory captured by the episode recorder
curl -X POST :8765/api/traj/load -d '{"arm":1,"name":"pour","points":[[...],[...]],"dt":0.05}'
curl -X POST :8765/api/traj/play -d '{"arm":1,"loop":false,"speed":0.5}'

# compliant mode with soft gains, then back to stiff position control
curl -X POST :8765/api/arm/mode -d '{"arm":0,"mode":"mit"}'
curl -X POST :8765/api/arm/mit  -d '{"arm":0,"kp":20,"kd":1.5}'
curl -X POST :8765/api/arm/mode -d '{"arm":0,"mode":"pos_j"}'
```
