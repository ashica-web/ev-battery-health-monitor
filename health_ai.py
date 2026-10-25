"""
health_ai.py - shared pieces of the AI layer.

Used by train_health_model.py (offline) and health_inference.py (live),
so both sides compute features, explanations and verdicts the same way.

WHAT THE AI DOES
  Estimates battery state of health (SOH) WITHOUT a full capacity test.
  A capacity test means a complete charge and discharge - hours per
  battery. The model instead reads three things any BMS already knows:

    voltage      mean cell voltage during a discharge   (V per cell)
    temperature  mean cell temperature during it        (deg C)
    cycle        charge cycles completed so far

  The model is gradient-boosted trees (XGBoost; falls back to
  scikit-learn's GradientBoosting if XGBoost is not installed).

WHY THIS IS NOT "AN IF-STATEMENT ON SOH"
  SOH is never an input. It is the thing being predicted, and it is
  checked against laboratory-measured capacity on batteries the model
  never saw while training.

EXPLANATIONS (SHAP)
  For every estimate we compute exact Shapley values: how many health
  points each input moved the estimate away from a NEW battery (the
  reference set is cycles above 97% health). So the reasons answer
  "why is this battery below a healthy one?"
  With three inputs there are only 8 combinations, so we compute them
  exactly instead of approximating - same quantity the shap library
  estimates, no extra dependency.
"""

import math

import numpy as np

FEATURES = ["voltage", "temperature", "cycle"]

# Verdict thresholds (percent health)
WARN_SOH = 85.0          # service recommended
EOL_SOH = 80.0           # end of first life (EV standard)
WARN_RUL_CYCLES = 25     # also warn if projected to hit 80% this soon

LABELS = {
    "voltage": "Discharge voltage",
    "temperature": "Cell temperature",
    "cycle": "Charge cycles",
}


# ------------------------------------------------------------------ model

def make_model():
    """XGBoost if available, otherwise scikit-learn. Same settings."""
    try:
        from xgboost import XGBRegressor
        return XGBRegressor(n_estimators=300, max_depth=3, learning_rate=0.05,
                            subsample=0.8, random_state=0), "XGBoost"
    except ImportError:
        from sklearn.ensemble import GradientBoostingRegressor
        return GradientBoostingRegressor(n_estimators=300, max_depth=3,
                                         learning_rate=0.05, subsample=0.8,
                                         random_state=0), "GradientBoosting (scikit-learn)"


def predict(model, X):
    """X: 2-D array in FEATURES order. Returns SOH as a fraction."""
    return np.asarray(model.predict(np.asarray(X, dtype=float)), dtype=float)


# ------------------------------------------------------------------ SHAP

def shapley(model, x, background):
    """
    Exact interventional Shapley values for one sample.

    x          1-D array, FEATURES order
    background 2-D array of typical samples (the "average battery")

    Returns (base_value, contributions) as SOH fractions, where
        base_value + sum(contributions) == prediction for x
    """
    x = np.asarray(x, dtype=float)
    bg = np.asarray(background, dtype=float)
    n = len(x)

    # value of every coalition S: mean prediction with features in S
    # fixed to x and the rest taken from the background rows
    values = {}
    for mask in range(1 << n):
        X = bg.copy()
        for j in range(n):
            if mask >> j & 1:
                X[:, j] = x[j]
        values[mask] = float(predict(model, X).mean())

    phi = np.zeros(n)
    for j in range(n):
        for mask in range(1 << n):
            if mask >> j & 1:
                continue
            s = bin(mask).count("1")
            w = math.factorial(s) * math.factorial(n - s - 1) / math.factorial(n)
            phi[j] += w * (values[mask | (1 << j)] - values[mask])
    return values[0], phi


def reasons(x, phi, ref, features=FEATURES):
    """
    Plain-language reasons, biggest effect first.
    x, phi in `features` order; ref = typical healthy-cell values.
    Each item: (feature, points, sentence)
    """
    val = dict(zip(features, x))
    out = []
    for f, p in zip(features, phi * 100):
        if f == "voltage":
            v = val[f]
            # wording follows the measured value; the points carry the effect
            if v < ref["voltage"] - 0.005:
                s = (f"Discharge voltage {v:.3f} V/cell has sagged below a healthy "
                     f"cell's {ref['voltage']:.3f} V")
            elif v > ref["voltage"] + 0.005:
                s = (f"Discharge voltage {v:.3f} V/cell is above a healthy "
                     f"cell's {ref['voltage']:.3f} V")
            else:
                s = (f"Discharge voltage {v:.3f} V/cell is close to a healthy "
                     f"cell's {ref['voltage']:.3f} V")
        elif f == "temperature":
            s = (f"Cell runs at {val[f]:.1f} C during discharge "
                 f"(healthy cells: about {ref['temperature']:.1f} C)")
        elif f == "cycle":
            s = f"{int(round(val[f]))} charge cycles completed"
        else:
            s = f"{LABELS.get(f, f)} {val[f]:.3g}"
        out.append((f, float(p), f"{s} ({p:+.1f} pts)"))
    out.sort(key=lambda r: -abs(r[1]))
    return out


def out_of_range(x, features, ranges, margin=0.0):
    """
    Inputs outside what the model saw in training. Tree models cannot
    extrapolate: outside the range they return the value at the edge, so
    the estimate and its explanation become unreliable. Returns messages.
    """
    msgs = []
    if not ranges:
        return msgs
    for f, v in zip(features, x):
        lo, hi = ranges.get(f, (None, None))
        if lo is None:
            continue
        span = (hi - lo) or 1.0
        if v < lo - margin * span or v > hi + margin * span:
            msgs.append(f"{LABELS.get(f, f)} {v:.3g} is outside the training range "
                        f"{lo:.3g}-{hi:.3g}")
    return msgs


# ------------------------------------------------------------------ RUL & verdict

def smooth(values, k=3):
    """Mean of the last k estimates - one noisy cycle can't flip the verdict."""
    vals = list(values)[-k:]
    return sum(vals) / len(vals) if vals else None


def remaining_cycles(history, min_points=6, window=20):
    """
    history: list of (cycle, soh_percent) estimates.
    Straight-line fit over the recent window -> cycles until 80%.
    None when there isn't a clear downward trend yet.
    """
    pts = history[-window:]
    if len(pts) < min_points:
        return None
    c = np.array([p[0] for p in pts], dtype=float)
    h = np.array([p[1] for p in pts], dtype=float)
    if c.max() - c.min() < 1:
        return None
    slope, _ = np.polyfit(c, h, 1)
    if slope >= -0.01:                 # less than 0.01 % per cycle: no trend
        return None
    now = smooth(h)
    if now <= EOL_SOH:
        return 0.0
    return float((now - EOL_SOH) / -slope)


def verdict(soh_smoothed, rul):
    if soh_smoothed is None:
        return "Normal"
    if soh_smoothed < EOL_SOH:
        return "Critical"
    if soh_smoothed < WARN_SOH or (rul is not None and rul < WARN_RUL_CYCLES):
        return "Warning"
    return "Normal"
