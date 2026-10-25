"""
cell_test_report.py - checks the AI against a real capacity test on YOUR pack.

After a full discharge on the rig (bridge running, so it was logged):

    python cell_test_report.py
    python cell_test_report.py --rated 2.6     if your label says 2600 mAh

  --rated     the cell's rated capacity in Ah, from its label (default 3.7)
  --csv       which day's log (default: the newest data/battery_*.csv)
  --pick N    which discharge in that file (default: the last long one)
  --save      also add the result to my_cells.csv for retraining

It finds the discharge in the log, then prints:

  measured capacity  (integrated current)   -> measured health
  AI estimate        (from voltage, temperature, cycles only)
  the difference, and why the AI said what it said

and saves cell_test_<time>.png for your slides.

Notes
  * 3 cells in series: the pack's capacity is the WEAKEST cell's capacity.
  * The discharge must run until the pack cuts off (about 3.0 V per cell),
    otherwise measured capacity is too low. The script warns you.
  * Use about 1 C (2-2.5 A for 2-2.5 Ah cells) - NASA's training rate.
"""

import argparse
import glob
import os
import pickle
import sys
from datetime import datetime

import numpy as np
import pandas as pd

import health_ai as H

HERE = os.path.dirname(os.path.abspath(__file__))
N_CELLS = 3
DISCHARGE_A = 0.3
MIN_DISCHARGE_S = 300
EMPTY_CELL_V = 3.15        # below this at the end = a full discharge


def per_cell_voltage(row):
    cells = [row.get(f"cell{i}_voltage") for i in range(1, N_CELLS + 1)]
    cells = [c for c in cells if pd.notna(c) and 2.0 < c < 4.5]
    if len(cells) == N_CELLS:
        return sum(cells) / N_CELLS
    p = row.get("pack_voltage")
    if pd.notna(p) and 2.0 * N_CELLS < p < 4.5 * N_CELLS:
        return p / N_CELLS
    return np.nan


def cell_temperature(row):
    ts = [row.get(f"cell{i}_temp") for i in range(1, N_CELLS + 1)]
    ts = [t for t in ts if pd.notna(t) and -40 < t < 100]
    return sum(ts) / len(ts) if ts else np.nan


def find_discharges(df):
    """Contiguous stretches with current below -DISCHARGE_A."""
    on = (df["current_A"] < -DISCHARGE_A).values
    t = df["timestamp"].values
    segs, start, quiet = [], None, 0
    for i, d in enumerate(on):
        if d:
            if start is None:
                start = i
            quiet, last = 0, i
        elif start is not None:
            quiet += 1
            if quiet >= 3:
                segs.append((start, last))
                start, quiet = None, 0
    if start is not None:
        segs.append((start, last))
    out = []
    for a, b in segs:
        dur = (t[b] - t[a]) / np.timedelta64(1, "s")
        if dur >= MIN_DISCHARGE_S:
            out.append((a, b, dur))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rated", type=float, default=3.7,
                    help="rated cell capacity in Ah from the label (default 3.7)")
    ap.add_argument("--csv")
    ap.add_argument("--pick", type=int)
    ap.add_argument("--save", action="store_true")
    a = ap.parse_args()

    path = a.csv
    if not path:
        files = sorted(glob.glob(os.path.join(HERE, "data", "battery_*.csv")))
        if not files:
            sys.exit("No logs in data/. Was the bridge (window 1) running during the test?")
        path = files[-1]
    df = pd.read_csv(path)
    df["timestamp"] = pd.to_datetime(df["timestamp"], format="ISO8601", utc=True)
    df = df.sort_values("timestamp").reset_index(drop=True)
    for c in df.columns:
        if c != "timestamp":
            df[c] = pd.to_numeric(df[c], errors="coerce")

    segs = find_discharges(df)
    print(f"\nLog: {os.path.basename(path)}")
    if not segs:
        sys.exit(f"No discharge of at least {MIN_DISCHARGE_S // 60} min found in this file.")
    for n, (s, e, dur) in enumerate(segs, 1):
        print(f"  discharge {n}: starts {df.timestamp[s].to_pydatetime().astimezone():%H:%M:%S}, {dur / 60:5.1f} min")
    pick = (a.pick or len(segs)) - 1
    if not 0 <= pick < len(segs):
        sys.exit(f"--pick must be 1..{len(segs)}")
    s, e, dur = segs[pick]
    seg = df.iloc[s:e + 1].copy()

    # ---- measured capacity: integrate the current ----
    dt = seg["timestamp"].diff().dt.total_seconds().fillna(0).clip(upper=5)
    amps = seg["current_A"].abs()
    ah = float((amps * dt).sum() / 3600)
    measured_health = ah / a.rated * 100

    # ---- AI inputs, built exactly like the live system ----
    v_cell = seg.apply(per_cell_voltage, axis=1)
    t_cell = seg.apply(cell_temperature, axis=1)
    volt, temp = float(v_cell.mean()), float(t_cell.mean())
    cyc = seg["cycles"].dropna()
    cycle = int(cyc.max()) + 1 if len(cyc) else 1
    end_v = float(v_cell.dropna().iloc[-5:].min()) if v_cell.notna().any() else np.nan
    mean_i = float(amps.mean())

    bundle_path = os.path.join(HERE, "health_model.pkl")
    if not os.path.exists(bundle_path):
        sys.exit("health_model.pkl missing - run: python train_health_model.py")
    with open(bundle_path, "rb") as f:
        B = pickle.load(f)
    feats = B.get("features", H.FEATURES)
    values = {"voltage": volt, "temperature": temp, "cycle": float(cycle)}
    x = np.array([values[f] for f in feats])
    est = float(H.predict(B["model"], [x])[0]) * 100
    base, phi = H.shapley(B["model"], x, B["background"])
    why = H.reasons(x, phi, B["ref"], feats)
    diff = est - measured_health
    fw = seg["soh_percent"].dropna()

    print("\n" + "=" * 62)
    print(f"  CELL TEST - discharge {pick + 1}, {dur / 60:.0f} min at {mean_i:.2f} A average")
    print("=" * 62)
    print(f"  measured capacity     {ah:.3f} Ah   (rated {a.rated} Ah)")
    print(f"  MEASURED HEALTH       {measured_health:5.1f} %")
    print(f"  AI ESTIMATE           {est:5.1f} %   (+/-{B['p90']:.0f} typical)")
    print(f"  difference            {diff:+5.1f} points")
    if len(fw):
        print(f"  firmware SOH          {fw.iloc[-1]:5.1f} %   (for reference)")
    print(f"\n  AI inputs: {volt:.3f} V/cell mean, {temp:.1f} C mean, cycle {cycle}")
    print("  Why:")
    for _, _, sentence in why:
        print(f"    - {sentence}")

    warnings = ["AI input " + m + " - estimate unreliable"
                for m in H.out_of_range(x, feats, B.get("ranges"))]
    if pd.notna(end_v) and end_v > EMPTY_CELL_V:
        warnings.append(f"Discharge ended at {end_v:.2f} V/cell - not empty. Measured health is "
                        f"TOO LOW; run until the pack cuts off (~3.0 V/cell).")
    c_rate = mean_i / a.rated
    if not 0.6 <= c_rate <= 1.4:
        warnings.append(f"Discharge rate {c_rate:.2f} C. The AI was trained at 1 C; at other "
                        f"rates its voltage input shifts and the estimate is less reliable.")
    if t_cell.isna().mean() > 0.5:
        warnings.append("Most temperature readings missing - check the DS18B20 probes.")
    if warnings:
        print("\n  CHECK:")
        for w in warnings:
            print(f"    ! {w}")

    # ---- plot for slides ----
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        mins = (seg["timestamp"] - seg["timestamp"].iloc[0]).dt.total_seconds() / 60
        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 6.5), sharex=True)
        for i in range(1, N_CELLS + 1):
            ax1.plot(mins, seg[f"cell{i}_voltage"], lw=1.4, label=f"cell {i}")
        ax1.set_ylabel("cell voltage (V)")
        ax1.legend(loc="lower left", fontsize=8)
        ax1.grid(alpha=.25)
        for i in range(1, N_CELLS + 1):
            ax2.plot(mins, seg[f"cell{i}_temp"].where(seg[f"cell{i}_temp"] > -40), lw=1.2)
        ax2.set_ylabel("cell temp (C)")
        ax2.set_xlabel("minutes")
        ax2.grid(alpha=.25)
        fig.suptitle(f"Our pack: measured health {measured_health:.1f}%  |  AI estimate "
                     f"{est:.1f}%  |  difference {diff:+.1f} pts", fontsize=12)
        fig.tight_layout()
        out = os.path.join(HERE, f"cell_test_{datetime.now():%Y%m%d_%H%M}.png")
        fig.savefig(out, dpi=130)
        print(f"\n  plot -> {os.path.basename(out)}")
    except ImportError:
        pass

    if a.save:
        mine = os.path.join(HERE, "my_cells.csv")
        new = not os.path.exists(mine)
        with open(mine, "a") as f:
            if new:
                f.write("battery_id,cycle,voltage,temperature,soh\n")
            f.write(f"own_pack,{cycle},{volt:.4f},{temp:.2f},{measured_health / 100:.4f}\n")
        print(f"  saved  -> my_cells.csv (retrain: python train_health_model.py --extra my_cells.csv)")


if __name__ == "__main__":
    main()
