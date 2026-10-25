"""
health_inference.py - the live AI layer.

Replaces ei_inference.py. For every discharge cycle it:

  1. builds the cycle's features from the 1-second MQTT stream
       voltage      mean per-cell voltage during the discharge
       temperature  mean cell temperature during the discharge
       cycle        charge cycles completed (from the firmware)
  2. ESTIMATES state of health with the trained model (no capacity test)
  3. EXPLAINS the estimate - exact SHAP values: how many health points
     each input moved it, turned into plain sentences
  4. PROJECTS cycles left until 80% from the trend of its own estimates
  5. Gives a verdict: Normal / Warning / Critical
  6. publishes everything to battery/ai

Health is the model's OUTPUT. It never reads the firmware's SOH; the
firmware's capacity-based SOH is only shown next to it, as the
measured value the estimate can be checked against.

Needs health_model.pkl - make it with:  python train_health_model.py

Run:  python -u health_inference.py
"""

import json
import os
import pickle
import sys
import threading
import time

import numpy as np
import paho.mqtt.client as mqtt

import health_ai as H

# ================= CONFIG =================
MQTT_HOST = os.environ.get("MQTT_HOST", "localhost")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
TOPIC_STATE = "battery/state"
TOPIC_TWIN = "battery/twin"
TOPIC_CMD = "battery/cmd/ai"          # send "RESET" to clear the trend history
TOPIC_OUT = "battery/ai"

HERE = os.path.dirname(os.path.abspath(__file__))
MODEL_PATH = os.path.join(HERE, "health_model.pkl")
HISTORY_PATH = os.path.join(HERE, "health_history.json")

N_CELLS = 3
DISCHARGE_A = 0.3          # |current| above this = discharging
QUIET_READINGS = 3         # this many idle readings end a discharge
MIN_DISCHARGE_S = 300      # a real discharge must last 5 min to count
MIN_REPLAY_S = 4           # replayed lab cycles are time-compressed
REPUBLISH_S = 30           # keep dashboards fresh between cycles
# ==========================================


def load_bundle():
    if not os.path.exists(MODEL_PATH):
        print("health_model.pkl not found.")
        print("Make it first:   python train_health_model.py")
        sys.exit(1)
    with open(MODEL_PATH, "rb") as f:
        return pickle.load(f)


B = load_bundle()
MODEL = B["model"]
FEATS = B.get("features", H.FEATURES)

lock = threading.Lock()
samples = []               # (per-cell V, temp C) during the current discharge
t_start = None
quiet = 0
cycle_in_progress = 0
source = "live"
r0_growth = None
last_result = None


def load_history():
    try:
        with open(HISTORY_PATH) as f:
            return [tuple(p) for p in json.load(f)]
    except (OSError, ValueError):
        return []


def save_history():
    try:
        with open(HISTORY_PATH, "w") as f:
            json.dump(history, f)
    except OSError:
        pass


history = load_history()      # [(cycle, est_soh_percent), ...]


# ---------------- features from one reading ----------------

def per_cell_voltage(d):
    cells = [d.get(f"cell{i}_voltage") for i in range(1, N_CELLS + 1)]
    cells = [c for c in cells if isinstance(c, (int, float)) and 2.0 < c < 4.5]
    if len(cells) == N_CELLS:
        return sum(cells) / N_CELLS
    pack = d.get("pack_voltage")
    if isinstance(pack, (int, float)) and 2.0 * N_CELLS < pack < 4.5 * N_CELLS:
        return pack / N_CELLS
    return None


def cell_temperature(d):
    ts = [d.get(f"cell{i}_temp") for i in range(1, N_CELLS + 1)]
    ts = [t for t in ts if isinstance(t, (int, float)) and -40 < t < 100]
    return sum(ts) / len(ts) if ts else None


# ---------------- one cycle -> estimate, explanation, verdict ----------------

def estimate_cycle(client, v, t, cycle, src):
    global history, last_result

    if history and cycle < history[-1][0]:
        print("  cycle count went backwards - new battery or new run, trend reset\n")
        history = []

    values = {"voltage": v, "temperature": t, "cycle": float(cycle)}
    x = np.array([values[f] for f in FEATS])
    est = float(H.predict(MODEL, [x])[0]) * 100
    base, phi = H.shapley(MODEL, x, B["background"])
    why = H.reasons(x, phi, B["ref"], FEATS)

    history.append((float(cycle), est))
    history = history[-200:]
    save_history()

    trend = H.smooth([h for _, h in history])
    rul = H.remaining_cycles(history)
    verdict = H.verdict(trend, rul)

    out = {
        "prediction": verdict,
        "est_soh": round(est, 2),
        "est_soh_trend": round(trend, 2),
        "uncertainty_pct": round(B["p90"], 1),
        "rul_cycles": round(rul, 0) if rul is not None else -1,
        "cycle": int(cycle),
        "base_soh": round(base * 100, 2),
        "feat_voltage": round(v, 4),
        "feat_temperature": round(t, 2),
        "source": src,
        "model": B["backend"],
        "kind": "cycle",
    }
    for f, p in zip(FEATS, phi * 100):
        out[f"shap_{f}"] = round(float(p), 2)
    for i, (_, _, sentence) in enumerate(why[:3], 1):
        out[f"reason{i}"] = sentence

    # Physical evidence from the physics twin - measured, not AI. Only
    # meaningful on the real pack, not on replayed lab data.
    unsure = H.out_of_range(x, FEATS, B.get("ranges"))
    if unsure:
        out["input_warning"] = "Estimate unreliable: " + "; ".join(unsure)

    if src != "replay" and r0_growth is not None:
        out["evidence"] = (f"Measured internal resistance {r0_growth:+.0f}% vs a new cell "
                           f"(physics twin)")

    last_result = out
    client.publish(TOPIC_OUT, json.dumps(out), retain=True)

    rul_txt = f"{rul:5.0f}" if rul is not None else "  n/a"
    print(f"  cycle {int(cycle):4d} | AI health {est:5.1f}% (trend {trend:5.1f}) "
          f"+/-{B['p90']:.0f} | cycles to 80% {rul_txt} | {verdict.upper()}")
    for _, _, sentence in why[:3]:
        print(f"      - {sentence}")
    if "evidence" in out:
        print(f"      - {out['evidence']}")
    if "input_warning" in out:
        print(f"      ! {out['input_warning']}")


def finish_discharge(client):
    global samples, t_start
    duration = time.time() - t_start if t_start else 0
    needed = MIN_REPLAY_S if source == "replay" else MIN_DISCHARGE_S
    vs = [s[0] for s in samples if s[0] is not None]
    ts = [s[1] for s in samples if s[1] is not None]
    if duration < needed or len(vs) < 3 or len(ts) < 3:
        print(f"  discharge of {duration:.0f}s ignored (needs {needed}s with voltage "
              f"and temperature readings)")
    else:
        estimate_cycle(client, sum(vs) / len(vs), sum(ts) / len(ts),
                       cycle_in_progress, source)
    samples, t_start = [], None


# ---------------- MQTT ----------------

def on_connect(client, userdata, flags, rc, properties=None):
    client.subscribe([(TOPIC_STATE, 0), (TOPIC_TWIN, 0), (TOPIC_CMD, 0)])
    print(f"connected to {MQTT_HOST}:{MQTT_PORT}\n")
    print("Waiting for a discharge - the AI estimates health when each one ends.\n")


def on_message(client, userdata, msg):
    global t_start, quiet, cycle_in_progress, source, r0_growth, history

    if msg.topic == TOPIC_CMD:
        if msg.payload.decode().strip().upper() == "RESET":
            with lock:
                history = []
                save_history()
            print("  trend history cleared\n")
        return

    try:
        d = json.loads(msg.payload.decode())
    except (json.JSONDecodeError, UnicodeDecodeError):
        return

    if msg.topic == TOPIC_TWIN:
        g = d.get("R0_growth_pct")
        if isinstance(g, (int, float)):
            r0_growth = float(g)
        return

    current = d.get("current_A")
    if not isinstance(current, (int, float)):
        return

    with lock:
        if current < -DISCHARGE_A:
            if t_start is None:
                t_start = time.time()
                source = d.get("source", "live")
            quiet = 0
            samples.append((per_cell_voltage(d), cell_temperature(d)))
            c = d.get("cycles")
            if isinstance(c, (int, float)):
                cycle_in_progress = int(c) + 1      # the cycle being run now
        elif t_start is not None:
            quiet += 1
            if quiet >= QUIET_READINGS:
                quiet = 0
                finish_discharge(client)


def republish_loop(client):
    while True:
        time.sleep(REPUBLISH_S)
        with lock:
            if last_result:
                held = dict(last_result, kind="hold")
                client.publish(TOPIC_OUT, json.dumps(held), retain=True)


def main():
    print("=" * 64)
    print("  AI health estimator - live")
    print("=" * 64)
    print(f"  model      {B['backend']}, trained without {B['demo_battery']}")
    print(f"  validated  {B['mae']:.1f} pts average error on {B['n_batteries']} unseen batteries "
          f"(cycle-count baseline {B['mae_baseline']:.1f})")
    print(f"  inputs     {', '.join(FEATS)}   -> output: state of health")
    print(f"  history    {len(history)} cycles loaded\n")

    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="health_ai")
    except AttributeError:
        client = mqtt.Client(client_id="health_ai")
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(MQTT_HOST, MQTT_PORT, 60)
    threading.Thread(target=republish_loop, args=(client,), daemon=True).start()
    try:
        client.loop_forever()
    except KeyboardInterrupt:
        print("\nstopped.")


if __name__ == "__main__":
    main()
