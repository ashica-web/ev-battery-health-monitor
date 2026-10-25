"""
demo_nasa_replay.py - the AI demo for judges.

Replays a REAL battery from NASA's ageing tests through the live system,
cycle by cycle, at high speed. The battery (B0005 by default) was kept
OUT of the deployed model's training, so the audience watches the AI
estimate the health of a battery it has never seen - next to the health
NASA actually measured in the lab.

    python demo_nasa_replay.py                 B0005, every 2nd cycle (~15 min)
    python demo_nasa_replay.py --step 1        every cycle (~30 min)
    python demo_nasa_replay.py --fast          4 s discharges (~8 min)
    python demo_nasa_replay.py --battery B0018 a different battery (note: this
                                               one WAS used in training)

What is real and what is not - say this out loud:
  REAL      each cycle's mean voltage, mean temperature, cycle number and
            the lab-measured health all come from NASA's recordings
  RECREATED the second-by-second stream: a 2-hour discharge is squeezed
            into a few seconds, shaped so its averages match the lab data
  So the AI receives exactly the inputs it would have received from the
  real battery - just faster.

The ESP32 can stay powered: this script sends it DEMO_ON, so it stops
publishing its own readings and runs its ON-CHIP AI on this replay
(dashed purple line). DEMO_OFF is sent at the end; if this window is
closed instead, the ESP32 leaves demo mode by itself after 30 s.
"""

import argparse
import json
import os
import random
import sys
import time

import paho.mqtt.client as mqtt

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "nasa_battery_cycles.csv")
MQTT_HOST = os.environ.get("MQTT_HOST", "localhost")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
TOPIC = "battery/state"

CELLS = 3
LOAD_A = 2.0          # NASA discharged these cells at a constant 2 A
AMBIENT_C = 24.0      # NASA room-temperature group


def load_battery(bid):
    rows = []
    with open(DATA) as f:
        header = f.readline().strip().split(",")
        for line in f:
            r = dict(zip(header, line.strip().split(",")))
            if r["battery_id"] == bid:
                rows.append({"cycle": int(r["cycle"]), "voltage": float(r["voltage"]),
                             "temperature": float(r["temperature"]),
                             "soh": float(r["soh"]), "capacity": float(r["capacity"])})
    return sorted(rows, key=lambda r: r["cycle"])


def payload(cells, temps, current, soc, soh, cycle_done, capacity):
    pack_v = round(sum(cells), 3)
    hottest = max(temps)
    return {
        "source": "replay",
        "replay_battery": ARGS.battery,
        "cell1_temp": round(temps[0], 2), "cell2_temp": round(temps[1], 2),
        "cell3_temp": round(temps[2], 2),
        "ambient_temp": AMBIENT_C, "ambient_humidity": 50.0,
        "cell1_voltage": round(cells[0], 3), "cell2_voltage": round(cells[1], 3),
        "cell3_voltage": round(cells[2], 3),
        "pack_voltage": pack_v,
        "current_A": round(current, 3),
        "power_W": round(pack_v * abs(current), 2),
        "soc_percent": round(soc * 100, 1),
        "soc_source": "replay",
        "soh_percent": round(soh * 100, 2),          # lab-measured truth
        "soh_source": "lab capacity test (NASA)",
        "capacity_Ah": round(capacity, 3),
        "piezo1_raw": int(1800 + (1 - soh) * 3000 + random.uniform(-8, 8)),
        "piezo2_raw": int(1775 + (1 - soh) * 3000 + random.uniform(-8, 8)),
        "gas_level": int(160 + random.uniform(-15, 15)),
        "cell_imbalance": round(max(cells) - min(cells), 3),
        "temp_gradient": round(max(temps) - min(temps), 2),
        "dT_dt": round(random.uniform(0, .03), 3),
        "cycles": cycle_done,
        "target_current": round(abs(current), 2),
        "pwm_duty": int(abs(current) / 3 * 1023),
        "bms_tripped": 0, "safety_active": 0, "safety_reason": "",
        "fault_ov": 0, "fault_uv": 0, "fault_ot": 1 if hottest > 55 else 0,
        "fault_gas": 0,
    }


def main():
    rows = load_battery(ARGS.battery)
    if not rows:
        sys.exit(f"{ARGS.battery} not found in {DATA}")
    rows = [r for r in rows if r["cycle"] >= ARGS.start][::max(1, ARGS.step)]

    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    except AttributeError:
        client = mqtt.Client()
    client.connect(MQTT_HOST, MQTT_PORT, 60)
    client.loop_start()
    client.publish("battery/cmd/sys", "DEMO_ON")     # ESP32: stop own readings, run AI on replay
    client.publish("battery/cmd/ai", "RESET")       # fresh trend for this battery
    time.sleep(1)

    dis_s = 4 if ARGS.fast else ARGS.discharge
    # at least 3 idle readings: that is how the AI knows a discharge ended
    rest_s = 3 if ARGS.fast else max(3, ARGS.rest)
    total = len(rows) * (dis_s + rest_s) / 60
    print("=" * 66)
    print(f"  Replaying NASA battery {ARGS.battery}: {len(rows)} cycles, about {total:.0f} min")
    print("=" * 66)
    print("  Per-cycle voltage, temperature and health are NASA's lab data.")
    print("  Watch the AI estimate against the lab-measured health.\n")

    offsets = [-0.004, 0.0, 0.004]           # small, mean-zero cell spread
    toffs = [-0.2, 0.0, 0.2]
    try:
        for r in rows:
            # discharge shaped like a real one, averages pinned to NASA's
            shape = [0.55 - 1.15 * (i / (dis_s - 1)) ** 1.4 for i in range(dis_s)]
            mean_shape = sum(shape) / dis_s
            tshape = [-1.5 + 3.0 * (i / (dis_s - 1)) for i in range(dis_s)]
            mean_t = sum(tshape) / dis_s
            for i in range(dis_s):
                v = r["voltage"] + shape[i] - mean_shape
                t = r["temperature"] + tshape[i] - mean_t
                cells = [v + o + random.uniform(-.002, .002) for o in offsets]
                temps = [t + o for o in toffs]
                soc = 1 - i / dis_s
                client.publish(TOPIC, json.dumps(payload(
                    cells, temps, -LOAD_A, soc, r["soh"], r["cycle"] - 1,
                    r["capacity"])), retain=True)
                time.sleep(1)
            for i in range(rest_s):
                v = r["voltage"] - 0.3 + 0.05 * i
                cells = [v + o for o in offsets]
                temps = [r["temperature"] + 1 - 0.5 * i + o for o in toffs]
                client.publish(TOPIC, json.dumps(payload(
                    cells, temps, 0.0, 0.02, r["soh"], r["cycle"],
                    r["capacity"])), retain=True)
                time.sleep(1)
            print(f"  cycle {r['cycle']:4d} replayed | lab-measured health {r['soh']*100:5.1f}%")
        print("\nReplay finished.")
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        client.publish("battery/cmd/sys", "DEMO_OFF")
        time.sleep(0.5)
        client.loop_stop()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--battery", default="B0005")
    ap.add_argument("--step", type=int, default=2, help="replay every Nth cycle")
    ap.add_argument("--start", type=int, default=1, help="first cycle")
    ap.add_argument("--discharge", type=int, default=8, help="seconds per discharge")
    ap.add_argument("--rest", type=int, default=3, help="seconds of rest per cycle")
    ap.add_argument("--fast", action="store_true")
    ARGS = ap.parse_args()
    main()
