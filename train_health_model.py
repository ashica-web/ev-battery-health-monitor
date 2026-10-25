"""
train_health_model.py - trains and validates the battery health estimator.

    python train_health_model.py
    python train_health_model.py --extra my_cells.csv
    python train_health_model.py --no-cycle      for cells with unknown history

What it does
  1. Loads NASA's battery ageing data (nasa_battery_cycles.csv)
  2. VALIDATES honestly: for every battery, trains on all the OTHER
     batteries and tests on that one ("leave-one-battery-out"). The model
     never sees the battery it is graded on.
  3. Replays each unseen battery cycle by cycle, exactly like the live
     system, and records when the Warning would have fired compared with
     when the battery actually reached end of life (80%).
  4. Compares against a no-AI baseline that only counts cycles.
  5. Trains the final model, holding out one battery (DEMO_BATTERY) so
     the live demo can replay a battery the model has never seen.
  6. Writes health_model.pkl, validation_report.txt/.csv/.png, and
     rebuilds the Grafana dashboards with the real numbers.

--no-cycle
  Drops cycle count from the inputs (voltage + temperature only). Use it
  for second-hand cells whose history nobody knows. Less accurate, and
  the report says by how much.

--extra my_cells.csv
  Your own cells, measured on your rig. Columns:
      battery_id,cycle,voltage,temperature,soh
  soh as a fraction (0.87) or percent (87). Each cell's true soh comes
  from a full capacity test. Added to training and to the validation.

Install once:  python -m pip install xgboost scikit-learn pandas numpy matplotlib
"""

import argparse
import json
import os
import pickle
import subprocess
import sys

import numpy as np
import pandas as pd

import health_ai as H

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "nasa_battery_cycles.csv")
MODEL_OUT = os.path.join(HERE, "health_model.pkl")
REPORT = os.path.join(HERE, "validation_report")

MIN_CYCLES = 60          # batteries with fewer cycles are too short to judge
DEMO_BATTERY = "B0005"   # kept out of the final model for the live replay


def load(extra=None):
    df = pd.read_csv(DATA)
    df = df[["battery_id", "cycle", "voltage", "temperature", "soh"]]
    df = df.groupby("battery_id").filter(lambda g: len(g) >= MIN_CYCLES)
    df["source"] = "NASA"
    if extra:
        mine = pd.read_csv(extra)
        missing = {"battery_id", "cycle", "voltage", "temperature", "soh"} - set(mine.columns)
        if missing:
            sys.exit(f"{extra} is missing columns: {', '.join(sorted(missing))}")
        if mine["soh"].max() > 1.5:
            mine["soh"] = mine["soh"] / 100.0
        mine["battery_id"] = mine["battery_id"].astype(str)
        mine["source"] = "own"
        df = pd.concat([df, mine[df.columns]], ignore_index=True)
        print(f"  added {len(mine)} rows from {extra} ({mine.battery_id.nunique()} cells)")
    return df.sort_values(["battery_id", "cycle"]).reset_index(drop=True)


FEATURES = list(H.FEATURES)


def fit(train, features=None):
    features = features or FEATURES
    model, backend = H.make_model()
    model.fit(train[features].values.astype(float), train["soh"].values)
    return model, backend


def replay(model, test):
    """Run one unseen battery cycle by cycle, like the live system."""
    est = H.predict(model, test[FEATURES].values) * 100
    hist, warn_cycle, crit_cycle = [], None, None
    for cyc, e in zip(test["cycle"].values, est):
        hist.append((float(cyc), float(e)))
        v = H.verdict(H.smooth([h for _, h in hist]), H.remaining_cycles(hist))
        if v != "Normal" and warn_cycle is None:
            warn_cycle = int(cyc)
        if v == "Critical" and crit_cycle is None:
            crit_cycle = int(cyc)
    return est, warn_cycle


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--extra", help="CSV of your own measured cells")
    ap.add_argument("--no-cycle", action="store_true",
                    help="inputs voltage + temperature only (unknown cell history)")
    args = ap.parse_args()
    if args.no_cycle:
        FEATURES.remove("cycle")

    print("=" * 64)
    print("  Battery health estimator - training and validation")
    print("=" * 64)
    df = load(args.extra)
    _, backend = H.make_model()
    print(f"  model: {backend}")
    print(f"  {df.battery_id.nunique()} batteries, {len(df)} cycles")
    print(f"  inputs: {', '.join(FEATURES)}   (health is the OUTPUT, never an input)\n")

    rows, all_err, all_err_base = [], [], []
    curves = {}
    for b in df.battery_id.unique():
        train, test = df[df.battery_id != b], df[df.battery_id == b]
        model, _ = fit(train)
        base, _ = fit(train, ["cycle"])
        est, warn_cycle = replay(model, test)
        est_base = H.predict(base, test[["cycle"]].values) * 100
        true = test["soh"].values * 100
        err = np.abs(est - true)
        err_base = np.abs(est_base - true)
        all_err.extend(err)
        all_err_base.extend(err_base)

        below = test[test.soh < H.EOL_SOH / 100]
        eol_cycle = int(below.cycle.iloc[0]) if len(below) else None
        health_at_warn = (float(test.loc[test.cycle == warn_cycle, "soh"].iloc[0]) * 100
                          if warn_cycle is not None else None)
        lead = (eol_cycle - warn_cycle) if (eol_cycle and warn_cycle) else None
        rows.append({
            "battery": b,
            "source": test.source.iloc[0],
            "cycles": len(test),
            "mean_error_pct": round(float(err.mean()), 2),
            "max_error_pct": round(float(err.max()), 2),
            "cycle_count_only_error_pct": round(float(err_base.mean()), 2),
            "warning_cycle": warn_cycle,
            "true_health_at_warning": None if health_at_warn is None else round(health_at_warn, 1),
            "end_of_life_cycle": eol_cycle,
            "warning_lead_cycles": lead,
        })
        curves[b] = (test["cycle"].values, true, est)

    rep = pd.DataFrame(rows)
    mae = float(np.mean(all_err))
    p90 = float(np.percentile(all_err, 90))
    mae_base = float(np.mean(all_err_base))
    leads = rep.warning_lead_cycles.dropna()
    early = rep[(rep.true_health_at_warning.notna()) & (rep.true_health_at_warning > 90)]
    missed = rep[rep.end_of_life_cycle.notna() & rep.warning_cycle.isna()]
    late = rep[rep.warning_lead_cycles.notna() & (rep.warning_lead_cycles < 0)]

    lines = [
        "BATTERY HEALTH ESTIMATOR - VALIDATION REPORT",
        "=" * 64,
        f"Model            {backend}",
        f"Inputs           {', '.join(FEATURES)}  (health is never an input)",
        f"Method           leave-one-battery-out: each battery is tested by a",
        f"                 model that was trained WITHOUT it",
        f"Batteries        {len(rep)}",
        "",
        "ACCURACY ON UNSEEN BATTERIES",
        f"  average error                 {mae:.1f} health points",
        f"  90% of estimates within       +/-{p90:.1f} points",
        f"  no-AI baseline (cycle count)  {mae_base:.1f} points",
        f"  improvement over baseline     {(1 - mae / mae_base) * 100:.0f}%",
        "",
        f"EARLY WARNING (Warning = estimate below {H.WARN_SOH:.0f}% or projected to",
        f"reach {H.EOL_SOH:.0f}% within {H.WARN_RUL_CYCLES} cycles)",
        f"  batteries that reached end of life      {rep.end_of_life_cycle.notna().sum()}",
        f"  warned before end of life               {(leads > 0).sum()}",
        f"  median warning lead                     {leads.median():.0f} cycles" if len(leads) else "  median warning lead                     n/a",
        f"  missed (no warning before 80%)          {len(missed) + len(late)}",
        f"  false alarms (warned above 90% health)  {len(early)}",
        "",
        "PER BATTERY",
        rep.to_string(index=False),
        "",
        "LIMITS - say these before a judge asks",
        "  * Trained on NASA lab cells (2 Ah 18650s, constant 2 A discharge).",
        "    Other cells, currents and temperatures need their own data: run",
        "    full capacity tests on your cells and retrain with --extra.",
        "  * 'cycle' assumes the cell's history is known. For second-hand cells",
        "    with unknown history, accuracy drops.",
        f"  * {DEMO_BATTERY} is excluded from the deployed model so the live demo",
        "    replays a battery the model has never seen.",
    ]
    report = "\n".join(lines)
    print(report)
    with open(REPORT + ".txt", "w") as f:
        f.write(report + "\n")
    rep.to_csv(REPORT + ".csv", index=False)

    # -------- plot: true vs estimated on each unseen battery --------
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        n = len(curves)
        cols = 4
        rws = int(np.ceil(n / cols))
        fig, axes = plt.subplots(rws, cols, figsize=(16, 3.4 * rws), sharey=True)
        for ax, (b, (c, t, e)) in zip(axes.flat, curves.items()):
            ax.plot(c, t, color="#2b6cb0", lw=2, label="Lab-measured health")
            ax.plot(c, e, color="#dd6b20", lw=1.6, ls="--", label="AI estimate")
            ax.axhline(H.EOL_SOH, color="#e53e3e", lw=1, alpha=.6)
            ax.axhline(H.WARN_SOH, color="#d69e2e", lw=1, alpha=.6)
            r = rep[rep.battery == b].iloc[0]
            ax.set_title(f"{b}  (unseen)  error {r.mean_error_pct:.1f} pts", fontsize=10)
            ax.set_xlabel("cycle")
            ax.grid(alpha=.25)
        for ax in list(axes.flat)[n:]:
            ax.axis("off")
        axes.flat[0].set_ylabel("health %")
        axes.flat[0].legend(fontsize=8, loc="lower left")
        fig.suptitle(f"AI health estimate vs lab measurement on batteries the model never saw  "
                     f"- average error {mae:.1f} pts (cycle-count baseline {mae_base:.1f})",
                     fontsize=12)
        fig.tight_layout()
        fig.savefig(REPORT + ".png", dpi=130)
        print(f"\n  plot  -> {os.path.basename(REPORT)}.png")
    except ImportError:
        print("\n  (matplotlib not installed - skipped the plot)")

    # -------- final model --------
    final_train = df[df.battery_id != DEMO_BATTERY]
    model, backend = fit(final_train)
    healthy = final_train[final_train.soh > 0.97]
    # Explanations are measured from a NEW battery: "why is this one below
    # a healthy cell?" - so the reference set is cycles above 97% health.
    bg = healthy.sample(min(120, len(healthy)), random_state=0)[FEATURES].values
    bundle = {
        "model": model,
        "backend": backend,
        "features": FEATURES,
        "background": bg,
        "ref": {f: float(healthy[f].mean()) for f in H.FEATURES},
        "ranges": {f: [float(df[f].min()), float(df[f].max())] for f in H.FEATURES},
        "mae": mae,
        "p90": p90,
        "mae_baseline": mae_base,
        "median_lead": None if not len(leads) else float(leads.median()),
        "n_batteries": int(len(rep)),
        "demo_battery": DEMO_BATTERY,
    }
    with open(MODEL_OUT, "wb") as f:
        pickle.dump(bundle, f)
    print(f"  model -> {os.path.basename(MODEL_OUT)}  (trained without {DEMO_BATTERY})")

    # plain numbers for the dashboards' talking points
    meta = {k: v for k, v in bundle.items() if k not in ("model", "background")}
    meta["early_warned"] = int((leads > 0).sum())
    meta["reached_eol"] = int(rep.end_of_life_cycle.notna().sum())
    meta["false_alarms"] = int(len(early))
    with open(os.path.join(HERE, "health_model_meta.json"), "w") as f:
        json.dump(meta, f, indent=2)

    # -------- rebuild dashboards so they quote these numbers --------
    for builder in ("export_model_to_c.py", "build_dashboard.py", "build_demo_dashboard.py",
                    "build_whatif_dashboard.py"):
        path = os.path.join(HERE, builder)
        if os.path.exists(path):
            subprocess.run([sys.executable, path], cwd=HERE)


if __name__ == "__main__":
    main()
