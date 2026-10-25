"""
fake_publisher.py (v2) - a simulated pack, for testing and rehearsing.

    python fake_publisher.py            normal pack
    python fake_publisher.py --fault    a 0.1 ohm loose connection appears
                                        after 15 minutes (the resistor demo,
                                        without touching hardware)

v2 uses the same physics (battery_model.py) as the digital twin, so the
voltages are physically consistent, and it behaves like real firmware:
  * the "firmware" SOC (soc_percent) is coulomb counting that assumes a
    full pack at boot and has a 0.04 A current-sensor offset - so it is
    WRONG and drifts, exactly the problem the Kalman filter fixes
  * the real state of charge is published as sim_true_soc, so in sim
    mode the dashboard can show truth vs coulomb counting vs Kalman
  * the load varies (2 A / 0.8 A / rest) like a drive, which lets the
    Kalman filter learn internal resistance

Stop: Ctrl+C
"""

import json
import math
import random
import sys
import time

import paho.mqtt.client as mqtt

from battery_model import CellParams, ThreveninCell

MQTT_HOST = "localhost"
MQTT_PORT = 1883
TOPIC = "battery/state"

FAULT = "--fault" in sys.argv
FAULT_AFTER_S = 15 * 60
FAULT_OHM = 0.10           # extra series resistance (whole pack)

TRUE_START_SOC = 0.88      # the pack is NOT full...
FIRMWARE_START = 100.0     # ...but firmware assumes it is at boot
CURRENT_OFFSET_A = 0.04    # current sensor reads this much high
CAPACITY_AH = 3.7       # keep equal to capacity_Ah in twin_engine.py

cells = [ThreveninCell(CellParams(capacity_Ah=CAPACITY_AH, R0_ohm=r))
         for r in (0.050, 0.052, 0.055)]          # cell 3 a little worse
for c in cells:
    c.reset(soc=TRUE_START_SOC, temp_C=27.0)
temp = [27.0, 27.2, 27.1]
ambient = 26.5
fw_soc = FIRMWARE_START
cycles = 0
t = 0


def load_profile(t):
    """2 A for 150 s, 0.8 A for 30 s, rest 30 s - repeat."""
    phase = t % 210
    return 2.0 if phase < 150 else (0.8 if phase < 180 else 0.0)


def build_payload(load):
    global fw_soc, t
    t += 1
    extra = FAULT_OHM if (FAULT and t >= FAULT_AFTER_S) else 0.0

    volts = []
    for i, c in enumerate(cells):
        v = c.step(load, ambient, 1.0) - load * extra / 3
        volts.append(round(v + random.gauss(0, 0.004), 3))
        heating = load * load * c.p.R0_aged * 1.2
        cooling = (temp[i] - ambient) * 0.02
        temp[i] += heating - cooling + random.uniform(-0.03, 0.03)

    measured_I = load + CURRENT_OFFSET_A + random.gauss(0, 0.01)
    fw_soc = max(0.0, fw_soc - measured_I / 3600 / CAPACITY_AH * 100)
    pack_v = round(sum(volts), 3)

    return {
        "cell1_temp": round(temp[0], 2),
        "cell2_temp": round(temp[1], 2),
        "cell3_temp": round(temp[2], 2),
        "ambient_temp": round(ambient + random.uniform(-0.2, 0.2), 2),
        "ambient_humidity": round(58 + random.uniform(-2, 2), 1),
        "cell1_voltage": volts[0],
        "cell2_voltage": volts[1],
        "cell3_voltage": volts[2],
        "pack_voltage": pack_v,
        "current_A": round(-measured_I, 3) if load > 0 else 0.0,
        "power_W": round(pack_v * load, 2),
        "soc_percent": round(fw_soc, 1),
        "sim_true_soc": round(cells[0].s.soc * 100, 2),
        "soh_percent": round(100.0 - cycles * 0.08, 1),
        "piezo1_raw": int(1850 + 40 * math.sin(t / 30) + random.uniform(-15, 15)),
        "piezo2_raw": int(1820 + 35 * math.sin(t / 27) + random.uniform(-15, 15)),
        "gas_level": int(380 + random.uniform(-25, 25)),
        "cell_imbalance": round(max(volts) - min(volts), 3),
        "temp_gradient": round(max(temp) - min(temp), 2),
        "dT_dt": round(random.uniform(0.0, 0.04), 3),
        "cycles": cycles,
        "target_current": round(load, 2),
        "pwm_duty": int(load / 3.0 * 1023),
        "bms_tripped": 0,
        "fault_ov": 0,
        "fault_uv": 1 if min(volts) < 2.8 else 0,
        "fault_ot": 1 if max(temp) > 55 else 0,
        "fault_gas": 0,
        "safety_active": 1 if (min(volts) < 2.8 or max(temp) > 55) else 0,
        "safety_reason": "",
    }


def main():
    global fw_soc, cycles
    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    except AttributeError:
        client = mqtt.Client()
    client.connect(MQTT_HOST, MQTT_PORT, 60)
    client.loop_start()
    client.publish("battery/cmd/sys", "DEMO_ON")   # real ESP32 (if on) goes quiet
    print(f"Publishing simulated pack to {MQTT_HOST}:{MQTT_PORT} on {TOPIC}")
    print(f"  real SOC starts at {TRUE_START_SOC*100:.0f}%, firmware SOC wrongly at "
          f"{FIRMWARE_START:.0f}% with a {CURRENT_OFFSET_A} A sensor offset")
    if FAULT:
        print(f"  a {FAULT_OHM} ohm loose connection appears after "
              f"{FAULT_AFTER_S // 60} minutes")
    print("Ctrl+C to stop.\n")

    try:
        while True:
            p = build_payload(load_profile(t))
            client.publish(TOPIC, json.dumps(p), retain=True)
            if t % 5 == 0:
                tag = "  [loose connection active]" if FAULT and t >= FAULT_AFTER_S else ""
                print(f"t {t:5d}s  real SOC {p['sim_true_soc']:5.1f}%  firmware SOC "
                      f"{p['soc_percent']:5.1f}%  {p['pack_voltage']:6.3f} V  "
                      f"{p['current_A']:6.2f} A{tag}")
            if cells[0].s.soc <= 0.05:
                print("\nPack empty - simulating a full recharge\n")
                for c in cells:
                    c.reset(soc=1.0, temp_C=temp[0])
                fw_soc = 100.0
                cycles += 1
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        client.publish("battery/cmd/sys", "DEMO_OFF")
        time.sleep(0.5)
        client.loop_stop()


if __name__ == "__main__":
    main()
