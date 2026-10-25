"""
export_model_to_c.py - puts the trained AI onto the ESP32.

    python export_model_to_c.py

Reads health_model.pkl and writes
    firmware/EV_SINGLE_ESP32/health_model.h
a plain-C copy of every decision tree, plus the explanation (SHAP)
reference set. The ESP32 then runs the SAME model with no laptop.

It also checks itself: it re-runs the C logic in Python on every NASA
cycle and compares with the original model. "max difference" should be
tiny (well under 0.01 health points). train_health_model.py runs this
automatically, so the header always matches the latest model.
"""

import json
import os
import pickle
import sys

import numpy as np
import pandas as pd

import health_ai as H

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "firmware", "EV_SINGLE_ESP32")
OUT = os.path.join(OUT_DIR, "health_model.h")


# ---------------------------------------------------------------- tree extraction
# Every tree becomes flat arrays. For node i:
#   feat[i]  input index, or -1 for a leaf
#   thr[i]   split threshold
#   left[i]  child when the test is true, right[i] otherwise
#   val[i]   leaf value (already scaled)
# split_le: True  -> test is  x <= thr   (scikit-learn)
#           False -> test is  x <  thr   (XGBoost)

def trees_xgboost(model):
    booster = model.get_booster()
    trees = []
    for js in booster.get_dump(dump_format="json"):
        root = json.loads(js)
        nodes = []

        def walk(n):
            idx = len(nodes)
            nodes.append(None)
            if "leaf" in n:
                nodes[idx] = (-1, 0.0, -1, -1, float(n["leaf"]))
                return idx
            kids = {c["nodeid"]: c for c in n["children"]}
            f = n["split"]
            fi = int(f[1:]) if f.startswith("f") and f[1:].isdigit() else H.FEATURES.index(f)
            yes = walk(kids[n["yes"]])
            no = walk(kids[n["no"]])
            nodes[idx] = (fi, float(n["split_condition"]), yes, no, 0.0)
            return idx

        walk(root)
        trees.append(nodes)
    return trees, False


def trees_sklearn(model):
    lr = model.learning_rate
    trees = []
    for est in model.estimators_[:, 0]:
        t = est.tree_
        nodes = []
        for i in range(t.node_count):
            if t.children_left[i] == -1:
                nodes.append((-1, 0.0, -1, -1, float(t.value[i].ravel()[0]) * lr))
            else:
                nodes.append((int(t.feature[i]), float(t.threshold[i]),
                              int(t.children_left[i]), int(t.children_right[i]), 0.0))
        trees.append(nodes)
    return trees, True


def raw_sum(trees, split_le, X):
    """The C logic, in Python, in float32 - used to check the export."""
    X = np.asarray(X, dtype=np.float32)
    out = np.zeros(len(X), dtype=np.float64)
    for nodes in trees:
        for r, x in enumerate(X):
            i = 0
            while nodes[i][0] != -1:
                f, thr, l, rr, _ = nodes[i]
                go_left = x[f] <= np.float32(thr) if split_le else x[f] < np.float32(thr)
                i = l if go_left else rr
            out[r] += nodes[i][4]
    return out


def main():
    path = os.path.join(HERE, "health_model.pkl")
    if not os.path.exists(path):
        sys.exit("health_model.pkl missing - run: python train_health_model.py")
    with open(path, "rb") as f:
        B = pickle.load(f)
    model = B["model"]
    feats = B.get("features", H.FEATURES)

    if B["backend"].startswith("XGBoost"):
        trees, split_le = trees_xgboost(model)
    else:
        trees, split_le = trees_sklearn(model)

    # Check against the real model on all NASA cycles. The constant
    # offset (base score / initial prediction) is measured, not assumed.
    df = pd.read_csv(os.path.join(HERE, "nasa_battery_cycles.csv"))
    X = df[feats].values.astype(float)
    pred = H.predict(model, X)
    raw = raw_sum(trees, split_le, X)
    offset = float(np.median(pred - raw))
    diff = np.abs(raw + offset - pred) * 100
    print(f"  {len(trees)} trees, {sum(len(t) for t in trees)} nodes, model {B['backend']}")
    print(f"  self-check on {len(X)} cycles: max difference {diff.max():.4f} health points")
    if diff.max() > 0.05:
        sys.exit("  EXPORT MISMATCH - do not flash this header. Send this output to Claude.")

    # ---------- write the header ----------
    flat_feat, flat_thr, flat_l, flat_r, flat_v, roots = [], [], [], [], [], []
    for nodes in trees:
        base = len(flat_feat)
        roots.append(base)
        for (f, thr, l, r, v) in nodes:
            flat_feat.append(f)
            flat_thr.append(thr)
            flat_l.append(base + l if l >= 0 else -1)
            flat_r.append(base + r if r >= 0 else -1)
            flat_v.append(v)
    bg = np.asarray(B["background"], dtype=float)

    def cf(v, digits=9):
        """C float literal - always has a '.' or exponent, so '1' -> '1.0f'."""
        t = f"{float(v):.{digits}g}"
        if not any(ch in t for ch in ".eEn"):
            t += ".0"
        return t + "f"

    def arr(ctype, name, vals, fmt):
        body = ", ".join(fmt(v) for v in vals)
        return f"static const {ctype} {name}[{len(vals)}] = {{{body}}};\n"

    ranges = B.get("ranges", {})
    lines = [
        "// health_model.h - GENERATED by export_model_to_c.py. Do not edit by hand.",
        f"// Model: {B['backend']}, {len(trees)} trees, inputs: {', '.join(feats)}",
        f"// Validated: {B['mae']:.1f} health points average error on {B['n_batteries']} unseen batteries.",
        "#pragma once",
        "#include <stdint.h>",
        "#include <string.h>",
        "",
        f"#define HM_N_FEATURES {len(feats)}",
        f"#define HM_N_TREES {len(trees)}",
        f"#define HM_N_BG {len(bg)}",
        f"#define HM_SPLIT_LE {1 if split_le else 0}",
        f"#define HM_OFFSET {cf(offset)}",
        f'#define HM_BACKEND "{B["backend"]}"',
        f"#define HM_UNCERTAINTY_PCT {B['p90']:.1f}f",
        "static const char* const HM_FEATURES[HM_N_FEATURES] = {"
        + ", ".join(f'"{f}"' for f in feats) + "};",
        "static const float HM_RANGE_LO[HM_N_FEATURES] = {"
        + ", ".join(cf(ranges.get(f, [-1e9, 1e9])[0], 7) for f in feats) + "};",
        "static const float HM_RANGE_HI[HM_N_FEATURES] = {"
        + ", ".join(cf(ranges.get(f, [-1e9, 1e9])[1], 7) for f in feats) + "};",
        "",
        arr("int16_t", "HM_ROOT", roots, str),
        arr("int8_t", "HM_FEAT", flat_feat, str),
        arr("float", "HM_THR", flat_thr, cf),
        arr("int16_t", "HM_LEFT", flat_l, str),
        arr("int16_t", "HM_RIGHT", flat_r, str),
        arr("float", "HM_VAL", flat_v, cf),
        arr("float", "HM_BG", bg.ravel().tolist(), lambda v: cf(v, 7)),
        r"""
// Estimated state of health as a FRACTION (0.87 = 87 %).
static inline float hm_predict(const float* x) {
  float s = HM_OFFSET;
  for (int t = 0; t < HM_N_TREES; t++) {
    int i = HM_ROOT[t];
    while (HM_FEAT[i] >= 0) {
      float v = x[HM_FEAT[i]];
#if HM_SPLIT_LE
      i = (v <= HM_THR[i]) ? HM_LEFT[i] : HM_RIGHT[i];
#else
      i = (v <  HM_THR[i]) ? HM_LEFT[i] : HM_RIGHT[i];
#endif
    }
    s += HM_VAL[i];
  }
  return s;
}

// Exact SHAP values: how much each input moved the estimate away from a
// new battery (the reference set). base + sum(phi) == hm_predict(x).
static inline void hm_shap(const float* x, float* phi, float* base) {
  const int n = HM_N_FEATURES;
  float v[1 << HM_N_FEATURES];
  float z[HM_N_FEATURES];
  for (int mask = 0; mask < (1 << n); mask++) {
    float acc = 0;
    for (int b = 0; b < HM_N_BG; b++) {
      for (int j = 0; j < n; j++)
        z[j] = ((mask >> j) & 1) ? x[j] : HM_BG[b * n + j];
      acc += hm_predict(z);
    }
    v[mask] = acc / HM_N_BG;
  }
  float fact[HM_N_FEATURES + 1];
  fact[0] = 1;
  for (int k = 1; k <= n; k++) fact[k] = fact[k - 1] * k;
  for (int j = 0; j < n; j++) {
    phi[j] = 0;
    for (int mask = 0; mask < (1 << n); mask++) {
      if ((mask >> j) & 1) continue;
      int s = __builtin_popcount(mask);
      float w = fact[s] * fact[n - s - 1] / fact[n];
      phi[j] += w * (v[mask | (1 << j)] - v[mask]);
    }
  }
  *base = v[0];
}

// Index of a named input, or -1 (e.g. "cycle" in a --no-cycle model).
static inline int hm_feature_index(const char* name) {
  for (int j = 0; j < HM_N_FEATURES; j++)
    if (strcmp(HM_FEATURES[j], name) == 0) return j;
  return -1;
}
""",
    ]
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(OUT, "w") as f:
        f.write("\n".join(lines))
    kb = os.path.getsize(OUT) / 1024
    print(f"  header -> {os.path.relpath(OUT, HERE)}  ({kb:.0f} KB source, offset {offset:.5f})")


if __name__ == "__main__":
    main()
