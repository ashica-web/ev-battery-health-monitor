"""
MQTT -> InfluxDB bridge  (v2 - stores everything)

Stores four topics, each as its own InfluxDB measurement:

  battery/state  -> "battery"  sensor readings        (+ daily CSV backup)
  battery/twin   -> "twin"     physics model output   (predicted V, residual)
  battery/ai     -> "ai"       AI health estimates
  battery/event  -> "event"    safety cutoffs, BMS trips

Grafana reads all four, so one dashboard can show everything.

Run:  python -u mqtt_to_influx.py
"""

import csv
import json
import os
import sys
from datetime import datetime, timezone

import paho.mqtt.client as mqtt
from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS

# ============ CONFIG ============
MQTT_HOST = "localhost"
MQTT_PORT = 1883

INFLUX_URL    = "http://localhost:8086"
INFLUX_TOKEN  = "battery-dev-token-change-me"
INFLUX_ORG    = "evbattery"
INFLUX_BUCKET = "telemetry"

PACK_ID = "pack01"
PRINT_EVERY = 5          # status line every N sensor messages
# ================================

TOPICS = {
    "battery/state": "battery",
    "battery/twin":  "twin",
    "battery/ai":    "ai",
    "battery/event": "event",
    "battery/ai_edge": "ai_edge",     # the same AI, running on the ESP32
}

CSV_FIELDS = [
    "cell1_temp", "cell2_temp", "cell3_temp",
    "ambient_temp", "ambient_humidity",
    "cell1_voltage", "cell2_voltage", "cell3_voltage",
    "pack_voltage", "current_A", "power_W",
    "soc_percent", "soh_percent",
    "piezo1_raw", "piezo2_raw", "gas_level",
    "cell_imbalance", "temp_gradient", "dT_dt",
    "cycles", "target_current", "pwm_duty",
    "bms_tripped", "fault_ov", "fault_uv", "fault_ot", "fault_gas",
]

HERE = os.path.dirname(os.path.abspath(__file__))
CSV_DIR = os.path.join(HERE, "data")

influx = None
write_api = None
csv_fh = None
csv_writer = None
csv_date = None
counts = {m: 0 for m in TOPICS.values()}
errors = 0


# ---------------- CSV (sensor data only) ----------------

def write_csv(ts, data):
    global csv_fh, csv_writer, csv_date
    today = datetime.now().strftime("%Y-%m-%d")
    if today != csv_date:
        if csv_fh:
            csv_fh.close()
        os.makedirs(CSV_DIR, exist_ok=True)
        path = os.path.join(CSV_DIR, f"battery_{today}.csv")
        new = not os.path.exists(path) or os.path.getsize(path) == 0
        csv_fh = open(path, "a", newline="")
        csv_writer = csv.writer(csv_fh)
        if new:
            csv_writer.writerow(["timestamp"] + CSV_FIELDS)
        csv_date = today

    csv_writer.writerow([ts.astimezone().isoformat(timespec="milliseconds")] +   # local time, e.g. +05:30
                        [data.get(f, "") for f in CSV_FIELDS])
    csv_fh.flush()


# ---------------- InfluxDB ----------------

def flatten(data, prefix=""):
    """Turn nested dicts into flat fields.
    {"classes": {"Normal": 0.8}} -> {"class_Normal": 0.8}"""
    out = {}
    for k, v in data.items():
        if isinstance(v, dict):
            short = "class" if k == "classes" else k
            for k2, v2 in v.items():
                out[f"{short}_{k2}"] = v2
        else:
            out[k] = v
    return out


def write_point(measurement, ts, data):
    p = Point(measurement).tag("pack", PACK_ID).time(ts, WritePrecision.MS)
    wrote = False
    for key, value in flatten(data).items():
        if key == "timestamp":
            continue
        if isinstance(value, bool):
            p = p.field(key, int(value)); wrote = True
        elif isinstance(value, (int, float)):
            p = p.field(key, float(value)); wrote = True
        elif isinstance(value, str):
            p = p.field(key, value); wrote = True
    if wrote:
        write_api.write(bucket=INFLUX_BUCKET, org=INFLUX_ORG, record=p)


# ---------------- MQTT ----------------

def on_connect(client, userdata, flags, rc, properties=None):
    client.subscribe([(t, 0) for t in TOPICS])
    print("MQTT connected, subscribed to:", ", ".join(TOPICS))


def on_message(client, userdata, msg):
    global errors
    measurement = TOPICS.get(msg.topic)
    if measurement is None:
        return

    ts = datetime.now(timezone.utc)
    try:
        data = json.loads(msg.payload.decode())
    except json.JSONDecodeError:
        errors += 1
        return

    try:
        write_point(measurement, ts, data)
        counts[measurement] += 1

        if measurement == "battery":
            write_csv(ts, data)
            if counts["battery"] % PRINT_EVERY == 0:
                print(f"  stored  sensor {counts['battery']:>6}  "
                      f"twin {counts['twin']:>6}  "
                      f"ai {counts['ai']:>5}  chip-ai {counts['ai_edge']:>4}  "
                      f"events {counts['event']:>3}")
        elif measurement == "event":
            print(f"  EVENT: {data.get('event', '?')}")

    except Exception as exc:
        errors += 1
        print(f"  write failed ({measurement}): {exc}")


def main():
    global influx, write_api
    print("=" * 56)
    print("  MQTT -> InfluxDB bridge  (sensor + twin + ai + events)")
    print("=" * 56)

    influx = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
    if not influx.ping():
        print("InfluxDB unreachable - is Docker running?")
        sys.exit(1)
    write_api = influx.write_api(write_options=SYNCHRONOUS)
    print("InfluxDB ok")

    try:
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    except AttributeError:
        c = mqtt.Client()
    c.on_connect = on_connect
    c.on_message = on_message
    c.connect(MQTT_HOST, MQTT_PORT, 60)

    print("Logging. Ctrl+C to stop.\n")
    try:
        c.loop_forever()
    except KeyboardInterrupt:
        print(f"\nStopped. {counts}  errors={errors}")
        if csv_fh:
            csv_fh.close()


if __name__ == "__main__":
    main()
