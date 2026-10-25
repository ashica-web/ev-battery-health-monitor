"""
test_kalman.py - proves the Kalman filter works, using a simulated cell
where the TRUE state of charge is known.

    python test_kalman.py

The test makes things deliberately hard, the way a real pack is:
  * the firmware's coulomb counter starts at 100% (it always assumes a
    full pack at boot) but the cell is really at 85%
  * the current sensor reads 0.05 A high (a typical ACS712 offset)
  * voltage readings have 10 mV of noise
  * the real cell has aged: 25% more resistance than the model assumes
    (the filter has to learn that resistance itself)
  * the load switches between 0, 1 and 2.5 A like a real drive

It prints the error of each method and saves kalman_test.png.
"""

import math
import random

from battery_model import CellParams, ThreveninCell
from kalman_soc import SocEKF

random.seed(1)
DT = 1.0
MINUTES = 60
TRUE_START = 0.85
CC_START = 1.00            # what the firmware assumes at boot
CURRENT_BIAS = 0.05        # A
V_NOISE = 0.010            # V

true_cell = ThreveninCell(CellParams(R0_ohm=0.045 * 1.25))   # aged cell
true_cell.reset(soc=TRUE_START)
model_params = CellParams()                                 # what the twin assumes
ekf = SocEKF(model_params)

cc = CC_START
rows = []
for k in range(int(MINUTES * 60 / DT)):
    t = k * DT
    # drive profile: 2 min at 2.5 A, 1 min at 1 A, 30 s idle, repeat
    phase = t % 210
    load = 2.5 if phase < 120 else (1.0 if phase < 180 else 0.0)

    v_true = true_cell.step(load, 25.0, DT)
    i_meas = load + CURRENT_BIAS + random.gauss(0, 0.01)
    v_meas = v_true + random.gauss(0, V_NOISE)

    cc = max(0.0, cc - i_meas * DT / (3600 * model_params.capacity_aged))
    if k == 0:
        ekf.start(v_meas, soc_guess=CC_START)   # same bad start as the firmware
    else:
        ekf.step(i_meas, v_meas, DT)
    rows.append((t / 60, true_cell.s.soc, cc, ekf.soc, ekf.sigma))

truth = [r[1] for r in rows]
err_cc = [abs(r[2] - r[1]) * 100 for r in rows]
err_ekf = [abs(r[3] - r[1]) * 100 for r in rows]
settle = next((r[0] for r, e in zip(rows, err_ekf) if e < 2.0), None)
inside = sum(abs(r[3] - r[1]) <= 2 * r[4] for r in rows[60:]) / len(rows[60:]) * 100

print("=" * 60)
print("  Kalman filter test - simulated cell, true SOC known")
print("=" * 60)
print(f"  true SOC: {TRUE_START*100:.0f}% -> {truth[-1]*100:.1f}% over {MINUTES} min")
print(f"  coulomb counting   final error {err_cc[-1]:5.1f} pts   average {sum(err_cc)/len(err_cc):5.1f}")
print(f"  Kalman filter      final error {err_ekf[-1]:5.1f} pts   average {sum(err_ekf)/len(err_ekf):5.1f}")
print(f"  Kalman within 2 pts of truth after {settle:.1f} min" if settle is not None
      else "  Kalman never got within 2 pts")
print(f"  truth inside the Kalman's own +/-2 sigma band {inside:.0f}% of the time")
print(f"  internal resistance learned {ekf.r0*1000:.1f} mOhm (true {true_cell.p.R0_aged*1000:.1f}, "
      f"model assumed {model_params.R0_aged*1000:.1f})")

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    m = [r[0] for r in rows]
    fig, ax = plt.subplots(figsize=(10, 5))
    ax.fill_between(m, [(r[3] - 2 * r[4]) * 100 for r in rows],
                    [(r[3] + 2 * r[4]) * 100 for r in rows],
                    color="#dd6b20", alpha=.18, label="Kalman uncertainty (±2σ)")
    ax.plot(m, [r[1] * 100 for r in rows], color="#2b6cb0", lw=2.4, label="True SOC")
    ax.plot(m, [r[2] * 100 for r in rows], color="#718096", lw=1.6, ls="--",
            label="Coulomb counting (firmware)")
    ax.plot(m, [r[3] * 100 for r in rows], color="#dd6b20", lw=1.8, label="Kalman filter")
    ax.set_xlabel("minutes")
    ax.set_ylabel("state of charge (%)")
    ax.set_ylim(0, 105)
    ax.grid(alpha=.25)
    ax.legend(loc="lower left")
    ax.set_title(f"Wrong start (100% vs real {TRUE_START*100:.0f}%) + {CURRENT_BIAS} A sensor "
                 f"offset: coulomb counting stays {err_cc[-1]:.0f} pts off, "
                 f"Kalman ends {err_ekf[-1]:.1f} pts off", fontsize=10)
    fig.tight_layout()
    fig.savefig("kalman_test.png", dpi=130)
    print("  plot -> kalman_test.png")
except ImportError:
    pass
