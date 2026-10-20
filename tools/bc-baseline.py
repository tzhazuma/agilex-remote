#!/usr/bin/env python3
"""
Behaviour-cloning baseline for the AgileX dual-arm rig.

This is deliberately NOT ACT. It is the smallest model that can prove the whole
pipeline is sound: exported episodes -> tensors -> a policy that learns
image + joint state -> commanded joint targets.

WHY IT IS WORTH RUNNING: with only a handful of demonstrations the useful
question is not "does it generalise" but "can it fit at all". If a small
vision-conditioned MLP cannot drive the training loss down on this data, then
something upstream is broken (misaligned frames, wrong columns, normalisation),
and no amount of ACT would help. Passing this is the gate to real training.

Reads the LeRobot-style export (parquet for state/action) plus the per-episode
JPEGs, so it validates BOTH halves of the export at once.

    /home/agilex/agilex-data/.venv-export/bin/python3 bc-baseline.py
"""
import argparse
import json
import os
import sys

import numpy as np
import torch
import torch.nn as nn
from PIL import Image

STATE_COLS = [f"L_j{i}" for i in range(6)] + ["L_grip"] + \
             [f"R_j{i}" for i in range(6)] + ["R_grip"]
ACTION_COLS = [f"L_t{i}" for i in range(6)] + ["L_tgrip"] + \
              [f"R_t{i}" for i in range(6)] + ["R_tgrip"]


def load(episodes_root, img_size):
    """Return (images, states, actions, episode_ids)."""
    eps = sorted(d for d in os.listdir(episodes_root)
                 if d.startswith("episode_") and
                 os.path.isdir(os.path.join(episodes_root, d)))
    imgs, sts, acts, eids = [], [], [], []
    for ei, name in enumerate(eps):
        d = os.path.join(episodes_root, name)
        tl = os.path.join(d, "timeline.csv")
        if not os.path.isfile(tl):
            continue
        import csv
        with open(tl, newline="") as f:
            rows = list(csv.DictReader(f))
        cam = os.path.join(d, "cam0")
        got = 0
        for r in rows:
            fno = int(r.get("frame") or 0)
            p = os.path.join(cam, "%06d.jpg" % fno)
            if not os.path.isfile(p):
                continue
            try:
                im = Image.open(p).convert("RGB").resize((img_size, img_size))
            except Exception:
                continue
            imgs.append(np.asarray(im, dtype=np.float32) / 255.0)
            sts.append([float(r.get(c) or 0.0) for c in STATE_COLS])
            acts.append([float(r.get(c) or 0.0) for c in ACTION_COLS])
            eids.append(ei)
            got += 1
        print("  %s: %d samples" % (name, got), flush=True)
    if not imgs:
        return None, None, None, None
    X = np.transpose(np.stack(imgs), (0, 3, 1, 2))          # NCHW
    S = np.asarray(sts, dtype=np.float32)
    A = np.asarray(acts, dtype=np.float32)
    return X, S, A, np.asarray(eids)


class BCPolicy(nn.Module):
    """Small CNN over one camera + MLP over joint state -> 14 joint targets."""

    def __init__(self, n_state, n_action):
        super().__init__()
        self.cnn = nn.Sequential(
            nn.Conv2d(3, 16, 5, 2, 2), nn.ReLU(),
            nn.Conv2d(16, 32, 5, 2, 2), nn.ReLU(),
            nn.Conv2d(32, 64, 5, 2, 2), nn.ReLU(),
            nn.AdaptiveAvgPool2d(1), nn.Flatten(),
        )
        self.head = nn.Sequential(
            nn.Linear(64 + n_state, 128), nn.ReLU(),
            nn.Linear(128, 128), nn.ReLU(),
            nn.Linear(128, n_action),
        )

    def forward(self, img, st):
        return self.head(torch.cat([self.cnn(img), st], dim=1))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--episodes", default="/home/agilex/agilex-data/episodes")
    ap.add_argument("--img-size", type=int, default=96)
    ap.add_argument("--steps", type=int, default=3000)
    ap.add_argument("--batch", type=int, default=64)
    ap.add_argument("--lr", type=float, default=1e-3)
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    torch.manual_seed(args.seed)
    dev = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print("device:", dev, "| torch", torch.__version__, flush=True)

    print("loading episodes...", flush=True)
    X, S, A, E = load(args.episodes, args.img_size)
    if X is None:
        print("no data"); return 1
    print("total samples: %d | image %s | state %s | action %s"
          % (len(X), X.shape[1:], S.shape[1:], A.shape[1:]), flush=True)

    # hold out the LAST episode so the split is by episode, not by frame -
    # splitting frames would leak nearly-identical neighbours into validation
    test_ep = int(E.max())
    tr = E != test_ep
    te = E == test_ep
    print("train %d samples (%d episodes) | val %d samples (episode %d)"
          % (tr.sum(), len(set(E[tr].tolist())), te.sum(), test_ep), flush=True)
    if te.sum() == 0:
        print("hold-out empty; falling back to a frame split"); te = np.arange(len(E)) % 5 == 0; tr = ~te

    Xt = torch.from_numpy(X)
    St = torch.from_numpy(S)
    At = torch.from_numpy(A)

    # normalise on the training split only
    s_mu, s_sd = St[tr].mean(0), St[tr].std(0) + 1e-6
    a_mu, a_sd = At[tr].mean(0), At[tr].std(0) + 1e-6
    Stn = (St - s_mu) / s_sd
    Atn = (At - a_mu) / a_sd

    model = BCPolicy(S.shape[1], A.shape[1]).to(dev)
    n_par = sum(p.numel() for p in model.parameters())
    print("policy params: %.2f M" % (n_par / 1e6), flush=True)

    opt = torch.optim.Adam(model.parameters(), lr=args.lr)
    lossf = nn.MSELoss()
    idx = np.where(tr)[0]
    ti = torch.from_numpy(np.where(te)[0])

    def evaluate():
        model.eval()
        with torch.no_grad():
            pred = model(Xt[ti].to(dev), Stn[ti].to(dev))
            # report in degrees, which is what matters to a human
            err = (pred - Atn[ti].to(dev)) * a_sd.to(dev)
            return lossf(pred, Atn[ti].to(dev)).item(), err.abs().mean().item()

    print("\nstep   train_mse   val_mse   val_mae_deg", flush=True)
    history = []
    for step in range(1, args.steps + 1):
        model.train()
        b = np.random.choice(idx, min(args.batch, len(idx)), replace=False)
        bi = torch.from_numpy(b)
        pred = model(Xt[bi].to(dev), Stn[bi].to(dev))
        loss = lossf(pred, Atn[bi].to(dev))
        opt.zero_grad(); loss.backward(); opt.step()

        if step % 300 == 0 or step == 1:
            vm, vd = evaluate()
            print("%5d  %10.5f  %8.5f  %11.3f" % (step, loss.item(), vm, vd), flush=True)
            history.append({"step": step, "train": loss.item(), "val": vm, "val_mae_deg": vd})

    # ---- the real test: can it fit the training set? ----
    model.eval()
    with torch.no_grad():
        pred_tr = model(Xt[torch.from_numpy(idx)].to(dev), Stn[torch.from_numpy(idx)].to(dev))
        tr_mse = lossf(pred_tr, Atn[torch.from_numpy(idx)].to(dev)).item()
        tr_deg = ((pred_tr - Atn[torch.from_numpy(idx)].to(dev)) * a_sd.to(dev)).abs().mean().item()
    vm, vd = evaluate()

    print("\n=== result ===", flush=True)
    print("  train MSE  : %.5f   (mean |err| %.3f deg)" % (tr_mse, tr_deg), flush=True)
    print("  val   MSE  : %.5f   (mean |err| %.3f deg)" % (vm, vd), flush=True)
    ok = tr_mse < 0.05
    print("  overfit gate (train MSE < 0.05): %s" % ("PASS" if ok else "FAIL"), flush=True)
    if not ok:
        print("  -> the model cannot even fit the training set; suspect the data,", flush=True)
        print("     not the model. Check frame/row alignment before training ACT.", flush=True)

    out = "/home/agilex/agilex-data/bc-baseline.json"
    with open(out, "w") as f:
        json.dump({"device": str(dev), "torch": torch.__version__,
                   "params": n_par, "samples": int(len(E)),
                   "train_samples": int(tr.sum()), "val_samples": int(te.sum()),
                   "history": history,
                   "final": {"train_mse": tr_mse, "train_mae_deg": tr_deg,
                             "val_mse": vm, "val_mae_deg": vd, "overfit_pass": bool(ok)}}, f, indent=2)
    print("  written:", out, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
