"""
control.py (v3) - load control panel, embedded in Grafana.

New in v3 (single ESP32, two-way control):
  * Digital-twin control: shows the current cap the twin set after a
    resistance jump, with a button to clear it (LIMIT:OFF)
  * Fan: automatic temperature curve or a manual setting (FAN:)
  * Shows the safety-cutoff reason and whether the ESP32 is in demo mode

New in v2:
  * Shows what the ESP32 actually reports back: its target current,
    PWM duty and measured current - so you can SEE whether a command
    arrived, instead of guessing
  * Diagnoses where the command chain breaks
  * "Live" mode: the slider sends immediately, no Apply button needed

Run:  python -m streamlit run control.py --server.port 8503 --server.headless true
"""

import json
import time

import paho.mqtt.client as mqtt
import streamlit as st
from streamlit_autorefresh import st_autorefresh

MQTT_HOST = "localhost"
MQTT_PORT = 1883

st.set_page_config(page_title="Load control", layout="centered")
st_autorefresh(interval=1500, key="tick")

st.markdown("""
<style>
  .stApp { background:#ffffff; color:#24292e; }
  [data-testid="stHeader"], footer, #MainMenu { display:none; }
  .block-container { padding:0.5rem 1rem 0 1rem !important; }
  h4 { color:#16181d !important; margin:0.3rem 0 0.2rem 0 !important; }
  .stButton button { border-radius:3px; border:1.5px solid #16181d;
                     background:#ffffff; color:#16181d; }
  .stButton button:hover { background:#16181d; color:#fff; border-color:#16181d; }
  .box { background:#f4f5f5; border:1px solid #dcdfe3; border-radius:3px;
         padding:8px 10px; font-size:13px; line-height:1.7;
         font-variant-numeric:tabular-nums; }
  .ok { color:#2b8a3e; } .warn { color:#d9480f; } .bad { color:#c92a2a; }
</style>
""", unsafe_allow_html=True)


@st.cache_resource
def connection():
    state = {"data": {}, "last_state_time": 0.0, "event": None,
             "sent": None, "sent_time": 0.0}

    def on_connect(c, u, f, rc, p=None):
        c.subscribe([("battery/state", 0), ("battery/event", 0)])

    def on_message(c, u, msg):
        try:
            d = json.loads(msg.payload.decode())
        except json.JSONDecodeError:
            return
        if msg.topic == "battery/state":
            state["data"] = d
            state["last_state_time"] = time.time()
        else:
            state["event"] = (time.strftime("%H:%M:%S"), d.get("event", "?"))

    try:
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    except AttributeError:
        c = mqtt.Client()
    c.on_connect = on_connect
    c.on_message = on_message
    c.connect(MQTT_HOST, MQTT_PORT, 60)
    c.loop_start()
    return c, state


client, s = connection()


def send_load(amps):
    client.publish("battery/cmd/load", f"{amps:.2f}")
    s["sent"] = round(amps, 2)
    s["sent_time"] = time.time()


# ---------------- controls ----------------
st.markdown("#### Load control")

live = st.toggle("Live — send as the slider moves", value=False)
amps = st.slider("Target discharge current (A)", 0.0, 3.0, 0.0, 0.1)

if live:
    if s["sent"] is None or abs(amps - s["sent"]) > 0.001:
        send_load(amps)
else:
    b1, b2 = st.columns(2)
    if b1.button(f"Apply {amps:.1f} A", use_container_width=True, type="primary"):
        send_load(amps)
    if b2.button("Release load", use_container_width=True):
        send_load(0.0)

# ---------------- feedback from the ESP32 ----------------
d = s["data"]
age = time.time() - s["last_state_time"] if s["last_state_time"] else None

if age is None or age > 5:
    status = '<span class="bad">No data from the pack — is the ESP32 online and on MQTT?</span>'
    target = pwm = meas = None
else:
    target = float(d.get("target_current", 0))
    pwm    = int(d.get("pwm_duty", 0))
    meas   = abs(float(d.get("current_A", 0)))

    since_sent = time.time() - s["sent_time"] if s["sent"] is not None else None

    if d.get("bms_tripped"):
        status = '<span class="bad">BMS cut the current — load released</span>'
    elif s["sent"] is not None and abs(target - s["sent"]) > 0.05 and since_sent and since_sent > 4:
        status = ('<span class="bad">Command not reaching the ESP32.</span> '
                  'Check its Serial Monitor for ">>> CMD from MQTT".')
    elif d.get("demo_mode"):
        status = '<span class="warn">ESP32 in DEMO MODE — load disabled while the replay runs</span>'
    elif d.get("safety_active"):
        status = (f'<span class="bad">Safety cutoff: {d.get("safety_reason") or "active"}</span>'
                  ' — load forced to 0 A')
    elif target > 0 and pwm == 0:
        status = ('<span class="warn">Target received but PWM held at 0</span> — '
                  'check the ESP32 Serial Monitor.')
    elif target > 0 and pwm > 300 and meas < 0.05:
        status = ('<span class="warn">PWM rising but no current flows</span> — '
                  'check MOSFET, load resistor, fuse and wiring.')
    elif target > 0 and abs(meas - target) <= 0.15:
        status = '<span class="ok">Tracking target</span>'
    elif target > 0:
        status = '<span class="warn">Ramping toward target…</span>'
    else:
        status = '<span class="ok">Idle — no load</span>'

fmt = lambda v, u, n=2: "—" if v is None else f"{v:.{n}f} {u}"
pwm_txt = "—" if pwm is None else f"{pwm} / 1023  ({pwm/1023*100:.0f}%)"
sent_txt = "—" if s["sent"] is None else f"{s['sent']:.2f} A"

event_txt = ""
if s["event"]:
    event_txt = f'<br>Last event: <span class="warn">{s["event"][1]}</span> at {s["event"][0]}'

st.markdown(f"""
<div class="box">
Sent from here: <b>{sent_txt}</b><br>
ESP32 target: <b>{fmt(target, "A")}</b><br>
PWM duty: <b>{pwm_txt}</b><br>
Measured current: <b>{fmt(meas, "A")}</b><br>
{status}{event_txt}
</div>
""", unsafe_allow_html=True)

# ---------------- calibration ----------------
st.markdown("#### Calibration")
k1, k2, k3 = st.columns(3)
if k1.button("SOC from V", use_container_width=True):
    client.publish("battery/cmd/sys", "SYNC")
if k2.button("Cycle done", use_container_width=True):
    client.publish("battery/cmd/sys", "CYCLE")
if k3.button("Zero current", use_container_width=True):
    client.publish("battery/cmd/sys", "CAL")

# ---------------- two-way: the digital twin acting on the pack ----------------
st.markdown("#### Digital twin control")
limit = d.get("current_limit_A") if d else None
fan_duty = d.get("fan_duty") if d else None
fan_mode = d.get("fan_mode", "auto") if d else "auto"

if limit is not None and float(limit) < 4.99:
    st.markdown(f'<div class="box"><span class="bad">Twin has limited the load to '
                f'{float(limit):.2f} A</span> after detecting a fault '
                f'(resistance jump). Find the cause before clearing it.</div>',
                unsafe_allow_html=True)
    if st.button("Clear twin limit", use_container_width=True):
        client.publish("battery/cmd/sys", "LIMIT:OFF")
        client.publish("battery/cmd/sys", "FAN:AUTO")
else:
    st.markdown('<div class="box"><span class="ok">No twin limit active</span> — '
                'the twin caps the load automatically if it detects a fault.</div>',
                unsafe_allow_html=True)

f1, f2 = st.columns([1, 2])
auto_fan = f1.toggle("Fan auto", value=(fan_mode == "auto"))
if auto_fan and fan_mode != "auto":
    client.publish("battery/cmd/sys", "FAN:AUTO")
if not auto_fan:
    pct = f2.slider("Fan speed (%)", 0, 100, 50, 10)
    if f2.button(f"Set fan {pct}%", use_container_width=True):
        client.publish("battery/cmd/sys", f"FAN:{pct}")
fan_txt = "—" if fan_duty is None else f"{int(fan_duty) / 255 * 100:.0f}% ({fan_mode})"
st.caption(f"Fan now: {fan_txt}")
