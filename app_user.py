"""
app_user.py (v2) - the CUSTOMER app, driven LIVE over MQTT.

Only three things a driver cares about:
  1. Battery health   - plain-language status, not a raw SOH number
  2. History          - trend since this app was opened (MQTT has no past -
                        see note below)
  3. Next service due - a date, worked out from that trend

Login is by VEHICLE NUMBER + PASSWORD.

IMPORTANT ABOUT "REAL-TIME VIA MQTT":
MQTT is a live stream, not a database - it only carries the CURRENT
reading, never yesterday's. So this app keeps its own rolling history in
memory, one sample a minute, starting from when it was opened. That is
what the History card and the service-date trend are built from. It
resets if the app restarts. If you want history that survives restarts
and goes back further, that has to come from a database (InfluxDB) -
this file is deliberately pure-MQTT as asked.

Run:  python -m streamlit run app_user.py --server.port 8505 --server.headless true
"""

import hashlib
import json
import os
import threading
import time
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import paho.mqtt.client as mqtt
import streamlit as st
from streamlit_autorefresh import st_autorefresh

# ================= CONFIG =================
# Override without editing this file:
#   set MQTT_HOST=192.168.1.7 && streamlit run app_user.py ...
# Lets the SAME file be deployed on a laptop that has no broker of its
# own, pointed at whichever machine is actually running Mosquitto.
MQTT_HOST = os.environ.get("MQTT_HOST", "localhost")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))

HERE = os.path.dirname(os.path.abspath(__file__))
USERS_PATH = os.path.join(HERE, "users.json")

SERVICE_THRESHOLD = 85.0     # below this, a service is recommended
EOL_THRESHOLD = 80.0         # below this, a service is required

HISTORY_SAMPLE_S = 60        # one history point per minute
HISTORY_MAX_POINTS = 1000    # about 16 hours at one point/minute

BG_COLOR = "#fdba90"
# ==========================================

st.set_page_config(page_title="My EV Battery", layout="centered",
                   initial_sidebar_state="collapsed")

st.markdown(f"""
<style>
  #MainMenu, footer, header {{visibility: hidden;}}
  [data-testid="stHeader"], [data-testid="stToolbar"] {{display: none;}}
  .stApp {{ background: {BG_COLOR}; }}
  .block-container {{ max-width: 480px; padding-top: 2rem; }}

  .card {{ background: #fff; border-radius: 16px; padding: 22px;
          box-shadow: 0 2px 10px rgba(0,0,0,.10); margin-bottom: 16px; }}
  .card h4 {{ margin: 0 0 12px 0; color: #4a2c14; font-size: 15px;
             font-weight: 700; text-transform: uppercase; letter-spacing: .04em; }}

  .veh-header {{ display:flex; justify-content:space-between; align-items:center;
                margin-bottom: 18px; }}
  .veh-header b {{ font-size: 22px; color: #3a2210; }}
  .veh-header span {{ color: #6b4226; font-size: 13px; }}

  .status-row {{ display:flex; align-items:center; gap: 18px; }}
  .ring {{ width: 96px; height: 96px; border-radius: 50%;
          display:flex; align-items:center; justify-content:center;
          font-size: 22px; font-weight: 700; color: #fff; flex-shrink:0; }}
  .status-text h2 {{ margin: 0; font-size: 19px; }}
  .status-text p {{ margin: 4px 0 0 0; color: #6b7280; font-size: 13.5px; line-height:1.5; }}

  .service-card.urgent {{ background: #fff2f0; border: 1px solid #ffc9c0; }}
  .service-card.ok {{ background: #f2fbf3; border: 1px solid #c8ecc9; }}
  .service-date {{ font-size: 20px; font-weight: 700; margin: 2px 0 6px 0; }}
  .service-reason {{ color: #6b7280; font-size: 13.5px; }}

  .live-dot {{ display:inline-block; width:8px; height:8px; border-radius:50%;
              margin-right:6px; background:#9ca3af; }}
  .live-dot.on {{ background:#22c55e; }}
</style>
""", unsafe_allow_html=True)

st_autorefresh(interval=2000, key="tick")


# ---------------- users ----------------

def load_users():
    if not os.path.exists(USERS_PATH):
        default = {
            "KA-01-AB-1234": {
                "password_sha256": hashlib.sha256(b"demo123").hexdigest(),
                "pack_id": "pack01",
                "owner": "Demo Vehicle",
            }
        }
        with open(USERS_PATH, "w") as f:
            json.dump(default, f, indent=2)
        return default
    with open(USERS_PATH) as f:
        return json.load(f)


def check_login(vehicle, password):
    users = load_users()
    v = vehicle.strip().upper()
    if v not in users:
        return None
    if users[v]["password_sha256"] != hashlib.sha256(password.encode()).hexdigest():
        return None
    return {"vehicle": v, **users[v]}


# ---------------- MQTT (one shared connection per process) ----------------
# NOTE: state is a plain dict captured in this closure, NOT st.session_state.
# Writing to st.session_state from the MQTT thread would silently fail;
# a plain dict works because the main thread just reads it each rerun.

@st.cache_resource
def mqtt_conn():
    state = {
        "connected": False,
        "latest": {},          # most recent battery/state payload
        "ai": {},               # most recent battery/ai payload
        "history": [],          # [(datetime, soh_percent), ...]
        "last_sample": 0.0,
        "lock": threading.Lock(),
    }

    def on_connect(c, u, flags, rc, properties=None):
        state["connected"] = (rc == 0)
        if rc == 0:
            c.subscribe([("battery/state", 0), ("battery/ai", 0)])

    def on_disconnect(c, u, rc, properties=None):
        state["connected"] = False

    def on_message(c, u, msg):
        try:
            payload = json.loads(msg.payload.decode())
        except json.JSONDecodeError:
            return

        with state["lock"]:
            if msg.topic == "battery/state":
                state["latest"] = payload
                now = time.time()
                if now - state["last_sample"] >= HISTORY_SAMPLE_S:
                    soh = payload.get("soh_percent")
                    if soh is not None:
                        state["history"].append((datetime.now(), float(soh)))
                        if len(state["history"]) > HISTORY_MAX_POINTS:
                            state["history"].pop(0)
                    state["last_sample"] = now
            elif msg.topic == "battery/ai":
                state["ai"] = payload

    try:
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    except AttributeError:
        c = mqtt.Client()
    c.on_connect = on_connect
    c.on_disconnect = on_disconnect
    c.on_message = on_message

    try:
        c.connect(MQTT_HOST, MQTT_PORT, 60)
        c.loop_start()
    except Exception:
        pass

    return c, state


client, mqtt_state = mqtt_conn()


# ---------------- health status ----------------

def health_status(soh, ai_prediction, has_fault):
    if has_fault:
        return ("Needs attention now", "#dc2626",
                "A safety alert is active. Please visit a service centre.")
    if soh is None:
        return ("Checking...", "#9ca3af",
                "Waiting for a live reading from your vehicle.")
    if soh < EOL_THRESHOLD:
        return ("Service required", "#dc2626",
                f"Battery health is at {soh:.0f}%. Please book a service soon.")
    if soh < SERVICE_THRESHOLD or ai_prediction in ("Warning", "Critical"):
        return ("Fair - keep an eye on it", "#d97706",
                "Your battery is ageing a little faster than usual. "
                "A check-up is recommended.")
    if soh < 95:
        return ("Good", "#16a34a", "Your battery is healthy.")
    return ("Excellent", "#15803d", "Your battery is in great condition.")


def next_service_date(history, current_soh, urgent):
    """history: list of (datetime, soh_percent) built live since the app opened."""
    if urgent:
        return "As soon as possible", "based on a current safety alert"

    if current_soh is not None and current_soh < SERVICE_THRESHOLD:
        return "As soon as possible", "your battery health is already below the recommended level"

    if len(history) < 5:
        due = datetime.now() + timedelta(days=180)
        return (due.strftime("%d %b %Y"),
                "standard 6-month check-up (still building your battery's live history)")

    t0 = history[0][0]
    days = np.array([(t - t0).total_seconds() / 86400.0 for t, _ in history])
    health = np.array([h for _, h in history])

    if days.max() - days.min() < 1e-6:
        due = datetime.now() + timedelta(days=180)
        return (due.strftime("%d %b %Y"),
                "standard 6-month check-up (still building your battery's live history)")

    slope, intercept = np.polyfit(days, health, 1)

    if slope >= -0.001:
        due = datetime.now() + timedelta(days=180)
        return (due.strftime("%d %b %Y"),
                "standard 6-month check-up (your battery isn't showing measurable wear yet)")

    latest_health = health[-1]
    days_to_threshold = (latest_health - SERVICE_THRESHOLD) / abs(slope)
    days_to_threshold = max(1, min(365, days_to_threshold))
    due = datetime.now() + timedelta(days=days_to_threshold)
    return due.strftime("%d %b %Y"), "estimated from your battery's live health trend"


# ---------------- login ----------------

if "user" not in st.session_state:
    st.session_state.user = None

if st.session_state.user is None:
    st.markdown("<br><br>", unsafe_allow_html=True)
    st.markdown("<h2 style='text-align:center;color:#3a2210'>My EV Battery</h2>",
               unsafe_allow_html=True)
    st.markdown("<p style='text-align:center;color:#6b4226'>Sign in with your "
               "vehicle number</p>", unsafe_allow_html=True)

    with st.form("login"):
        vehicle = st.text_input("Vehicle number", placeholder="e.g. KA-01-AB-1234")
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Sign in", use_container_width=True)

    if submitted:
        user = check_login(vehicle, password)
        if user:
            st.session_state.user = user
            st.rerun()
        else:
            st.error("Vehicle number or password not recognised.")

    st.caption("Demo login: KA-01-AB-1234 / demo123")
    st.stop()


# ---------------- dashboard ----------------

user = st.session_state.user

with mqtt_state["lock"]:
    latest = dict(mqtt_state["latest"])
    ai = dict(mqtt_state["ai"])
    history = list(mqtt_state["history"])
connected = mqtt_state["connected"]

dot_cls = "on" if connected else ""
st.markdown(f'''
<div class="veh-header">
  <div><b>{user['vehicle']}</b><br><span>{user.get('owner','')}</span></div>
  <div><span class="live-dot {dot_cls}"></span>
       <span style="font-size:12px;color:#6b4226">
       {"Live" if connected else "Connecting..."}</span></div>
</div>
''', unsafe_allow_html=True)

if st.button("Sign out"):
    st.session_state.user = None
    st.rerun()

soh = latest.get("soh_percent")
soh = float(soh) if soh is not None else None
ai_pred = ai.get("prediction")
has_fault = any(latest.get(k) == 1 for k in
               ("fault_ov", "fault_uv", "fault_ot", "fault_gas",
                "bms_tripped", "safety_active"))

label, color, message = health_status(soh, ai_pred, has_fault)

# ---- Battery health ----
st.markdown(f'''
<div class="card">
  <h4>Battery health</h4>
  <div class="status-row">
    <div class="ring" style="background:{color}">
      {f"{soh:.0f}%" if soh is not None else "--"}
    </div>
    <div class="status-text">
      <h2 style="color:{color}">{label}</h2>
      <p>{message}</p>
    </div>
  </div>
</div>
''', unsafe_allow_html=True)

# ---- History ----
st.markdown('<div class="card"><h4>History</h4>', unsafe_allow_html=True)
if len(history) >= 2:
    hist_df = pd.DataFrame(history, columns=["time", "health"]).set_index("time")
    st.line_chart(hist_df["health"], height=180)
    span_min = (history[-1][0] - history[0][0]).total_seconds() / 60
    st.caption(f"Live trend over the last {span_min:.0f} minutes")
else:
    st.caption("Your battery's live history builds up here the longer this "
               "app stays open.")
st.markdown('</div>', unsafe_allow_html=True)

# ---- Next service ----
due_date, reason = next_service_date(history, soh, has_fault)
urgent = has_fault or (soh is not None and soh < SERVICE_THRESHOLD)
card_class = "urgent" if urgent else "ok"

st.markdown(f'''
<div class="card service-card {card_class}">
  <h4>Next service due</h4>
  <div class="service-date" style="color:{'#dc2626' if urgent else '#16a34a'}">{due_date}</div>
  <div class="service-reason">{reason}</div>
</div>
''', unsafe_allow_html=True)

if urgent:
    st.button("Find nearest service centre", use_container_width=True, type="primary")
