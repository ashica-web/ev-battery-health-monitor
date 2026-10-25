"""
twin_engine.py  (v4)
====================
Runs the physics model in parallel with the physical pack.

NEW IN v4 - Kalman filter state estimation (kalman_soc.py):
  An Extended Kalman Filter runs alongside the model. Every second it
  predicts SOC from the current, then corrects it with the measured
  voltage, and learns the internal resistance as it goes. It publishes
  ekf_soc (+/- ekf_soc_sigma) next to the firmware's coulomb-counted
  SOC, so the dashboard shows the filter fixing coulomb-counting drift.
  Run  python test_kalman.py  to see it proven on a simulated cell.

NEW IN v3 - self-calibrating R0:
  A fixed R0 guess only describes a fresh cell. Once your pack has
  aged (SOH 87.5% in your last log, real R0 ~50 mOhm measured), a
  model still assuming a NEW cell's resistance predicts too HIGH a
  voltage under load, and the residual grows every cycle even though
  nothing is actually wrong - that was the "drifting" you were seeing.

  So the model's own R0 now tracks what is actually being measured
  from load steps, smoothed over several readings so one noisy sample
  can't swing it. The ORIGINAL value stays fixed as R0_BASELINE_OHM,
  used only to report % growth for the "new cell vs now" comparison -
  it is never itself changed.

Fixes carried over from v2:
  * The model stays idle until real voltage arrives, so sensor noise
    before the pack is connected can't drain its SOC to zero
  * Checks measured voltage is physically plausible before trusting it
  * Re-seeds automatically if it is clearly out of step while the
    pack is resting (e.g. after the sensors were rewired)
  * Reports "no_data" instead of a stale "divergent" when unwired

Status values published to battery/twin:
  no_data      voltage sensor not connected
  implausible  measured voltage outside what 3 real cells can produce
  tracking     model and pack agree
  drifting     moderate disagreement
  divergent    strong disagreement - degradation or a fault

Run:  python -u twin_engine.py
"""

import json
import time
from collections import deque

import paho.mqtt.client as mqtt

from battery_model import BatteryPack, CellParams, soc_from_ocv
from kalman_soc import SocEKF

# ================= CONFIG =================
MQTT_HOST = "localhost"
MQTT_PORT = 1883
TOPIC_IN  = "battery/state"
TOPIC_OUT = "battery/twin"

N_CELLS = 3

PARAMS = CellParams(
    capacity_Ah=3.7,     # YOUR cells' rated capacity in Ah (from the label: 3700 mAh = 3.7)
    R0_ohm=0.045,        # starting guess for a NEW cell - see R0_BASELINE_OHM below
    R1_ohm=0.020,
    C1_farad=800.0,
)
# Fixed reference point for "% grown from new" - captured once, at
# start-up, and never itself changed. PARAMS.R0_ohm below IS allowed
# to change (it's what the model actually uses each step).
R0_BASELINE_OHM = PARAMS.R0_ohm

# Physically possible range for one lithium-ion cell
CELL_V_MIN_PLAUSIBLE = 2.5
CELL_V_MAX_PLAUSIBLE = 4.35

RESIDUAL_WARN_V  = 0.15
RESIDUAL_ALARM_V = 0.35

# Re-seed if |residual| exceeds this for RESEED_COUNT readings at rest
RESEED_THRESHOLD_V = 0.5
RESEED_COUNT = 20
REST_CURRENT_A = 0.1

# Self-calibration: start adjusting the model's R0 once this many good
# measurements are in, and only move it a fraction of the way each
# time so a single noisy step can't yank it around.
R0_CALIBRATE_MIN_SAMPLES = 3
R0_CALIBRATE_STEP = 0.3
R0_CALIBRATE_DEADBAND_OHM = 0.003     # ignore changes smaller than 3 mOhm

# Sync the physics twin's SOC to the Kalman filter once the filter is
# confident (one standard deviation below this, as a fraction)
EKF_SYNC_SIGMA = 0.03

# Resistance-jump alarm: the Kalman filter's R0 rising this much above
# its lowest value in the last JUMP_WINDOW_S seconds means a connection
# or cell has suddenly got worse (loose busbar, corroded contact, damaged
# cell) - ageing never moves this fast. Status shows it for JUMP_HOLD_S.
JUMP_RATIO = 1.20
JUMP_WINDOW_S = 300
JUMP_HOLD_S = 120
JUMP_COOLDOWN_S = 900     # one alarm per fault: the estimate takes ~10 min to settle
JUMP_WARMUP_S = 600       # no alarms while the filter is still learning R0
TOPIC_EVENT = "battery/event"

# TWO-WAY CONTROL: on a resistance jump the twin acts on the real pack -
# it caps the load current (LIMIT) and runs the fan flat out, then tells
# everyone what it did. Clear it from the control app ("Clear twin limit").
AUTO_PROTECT = True
PROTECT_LIMIT_A = 0.5
TOPIC_CMD_SYS = "battery/cmd/sys"
# ==========================================

pack = BatteryPack(n_cells=N_CELLS, params=PARAMS)

# The Kalman filter keeps its OWN copy of the parameters: it learns R0
# as one of its states, independently of the step-based calibration
# below, so the two act as a cross-check on each other.
ekf = SocEKF(CellParams(capacity_Ah=PARAMS.capacity_Ah, R0_ohm=PARAMS.R0_ohm,
                        R1_ohm=PARAMS.R1_ohm, C1_farad=PARAMS.C1_farad))

initialised = False
last_time = None
residual_window = deque(maxlen=60)
bad_streak = 0
msg_count = 0

prev_load = 0.0
prev_voltage = 0.0
r0_estimates = deque(maxlen=20)

ekf_r0_history = deque()        # (time, R0) for the jump alarm
jump_until = 0.0                # status shows the alarm until this time
last_jump = -1e9
ekf_started = None
jump_info = {}


def seed_from(measured_V, temp_C):
    """Set the model's SOC from the measured resting voltage."""
    global initialised, bad_streak
    soc = soc_from_ocv(measured_V / N_CELLS)
    pack.reset(soc=soc, temp_C=temp_C)
    residual_window.clear()
    bad_streak = 0
    initialised = True
    return soc


def publish(client, payload):
    client.publish(TOPIC_OUT, json.dumps(payload), retain=True)


def on_connect(client, userdata, flags, rc, properties=None):
    client.subscribe(TOPIC_IN)
    print(f"connected, subscribed to {TOPIC_IN}\n")


def check_resistance_jump(client, now):
    """Sudden R0 rise -> alarm + event. Returns True while the alarm holds."""
    global jump_until, jump_info, last_jump, ekf_started
    if ekf.sigma >= EKF_SYNC_SIGMA:
        return False                                  # filter not settled yet
    if ekf_started is None:
        ekf_started = now
    if now - ekf_started < JUMP_WARMUP_S:
        return False                                  # still learning this pack's R0
    ekf_r0_history.append((now, ekf.r0))
    while ekf_r0_history and now - ekf_r0_history[0][0] > JUMP_WINDOW_S:
        ekf_r0_history.popleft()
    low = min(r for _, r in ekf_r0_history)
    if (now - last_jump >= JUMP_COOLDOWN_S                  # one alarm per fault
            and ekf.r0 > JUMP_RATIO * low and len(ekf_r0_history) > 30):
        last_jump = now
        jump_until = now + JUMP_HOLD_S
        jump_info = {"from_mOhm": round(low * 1000, 1), "to_mOhm": round(ekf.r0 * 1000, 1),
                     "rise_pct": round((ekf.r0 / low - 1) * 100, 0)}
        ev = {"event": "RESISTANCE_JUMP", **jump_info}
        client.publish(TOPIC_EVENT, json.dumps(ev))
        print(f"  !!! RESISTANCE JUMP: {jump_info['from_mOhm']} -> "
              f"{jump_info['to_mOhm']} mOhm per cell (+{jump_info['rise_pct']:.0f}%) "
              f"- loose connection or damaged cell?")
        if AUTO_PROTECT:
            client.publish(TOPIC_CMD_SYS, f"LIMIT:{PROTECT_LIMIT_A}")
            client.publish(TOPIC_CMD_SYS, "FAN:100")
            client.publish(TOPIC_EVENT, json.dumps({
                "event": "TWIN_ACTION",
                "action": f"load limited to {PROTECT_LIMIT_A} A, fan 100%",
                "reason": "resistance jump"}))
            jump_info["action"] = f"load limited to {PROTECT_LIMIT_A} A, fan 100%"
            print(f"  >>> TWIN ACTION: load limited to {PROTECT_LIMIT_A} A, fan to 100%")
    return now < jump_until


_safety_was = False


def log_safety_change(client, d):
    """The ESP32 reports its own safety cutoff in every reading
    (safety_active / safety_reason). Record the moment it starts and ends
    as events, so the dashboard's event log has a timestamp for each."""
    global _safety_was
    active = str(d.get("safety_active", 0)).lower() in ("1", "true")
    if active and not _safety_was:
        client.publish(TOPIC_EVENT, json.dumps({
            "event": "SAFETY_CUTOFF", "action": "ESP32 switched the load off",
            "reason": str(d.get("safety_reason") or "limit exceeded")}))
        print(f"  [EVENT] ESP32 safety cutoff: {d.get('safety_reason')}")
    elif _safety_was and not active:
        client.publish(TOPIC_EVENT, json.dumps({
            "event": "SAFETY_CLEARED", "action": "load allowed again",
            "reason": "readings back inside limits"}))
    _safety_was = active


def on_message(client, userdata, msg):
    global last_time, msg_count, bad_streak, prev_load, prev_voltage

    try:
        d = json.loads(msg.payload.decode())
    except json.JSONDecodeError:
        return

    now = time.time()
    dt = 1.0 if last_time is None else min(now - last_time, 5.0)
    last_time = now
    msg_count += 1

    log_safety_change(client, d)

    measured_V = float(d.get("pack_voltage") or 0.0)
    load_A     = abs(float(d.get("current_A") or 0.0))
    ambient_C  = float(d.get("ambient_temp") or 25.0)
    measured_T = float(d.get("cell1_temp") or 25.0)
    per_cell   = measured_V / N_CELLS

    # ---------- 0. replayed lab data (demo_nasa_replay.py) ----------
    # Each 2-hour lab discharge is squeezed into seconds, so a physics
    # model running in real time cannot - and should not - track it.
    # Say so plainly instead of reporting a false "divergent".
    if d.get("source") == "replay":
        publish(client, {"status": "replay", "measured_V": round(measured_V, 4),
                         "measured_temp_C": round(measured_T, 2)})
        if msg_count % 30 == 0:
            print("  replayed lab data - physics twin paused")
        return

    # ---------- 1. no voltage sensor ----------
    if measured_V < 1.0:
        publish(client, {"status": "no_data", "measured_V": measured_V,
                         "measured_temp_C": round(measured_T, 2)})
        if msg_count % 10 == 0:
            print("  no voltage yet - model idle")
        return

    # ---------- 2. physically impossible reading ----------
    if not (CELL_V_MIN_PLAUSIBLE <= per_cell <= CELL_V_MAX_PLAUSIBLE):
        publish(client, {"status": "implausible",
                         "measured_V": round(measured_V, 4),
                         "per_cell_V": round(per_cell, 4),
                         "measured_temp_C": round(measured_T, 2)})
        if msg_count % 10 == 0:
            print(f"  IMPLAUSIBLE: {measured_V:.3f} V = {per_cell:.3f} V/cell "
                  f"- check wiring or TAP3_FACTOR with a multimeter")
        return

    # ---------- 3. first good reading: seed the model ----------
    if not initialised:
        soc = seed_from(measured_V, measured_T)
        print(f"model seeded: {measured_V:.3f} V -> SOC {soc*100:.1f}%")

    # ---------- 3b. Kalman filter: predict with current, correct with voltage ----------
    signed_I = -float(d.get("current_A") or 0.0)      # + = discharging
    ekf.step(signed_I, per_cell, dt)

    # Keep the twin's SOC synchronised to the filter once it has settled.
    # Without this, a SOC seeded from a voltage taken under load stays
    # wrong forever and shows up as a false "drifting" residual. With it,
    # the residual only grows when the pack really behaves differently
    # from the model - a resistance jump, a weak cell, a fault.
    if ekf.sigma < EKF_SYNC_SIGMA:
        for cell in pack.cells:
            cell.s.soc = ekf.soc

    # ---------- 4. run the model with the real current ----------
    predicted_V = pack.step(load_A, ambient_C, dt)
    residual = measured_V - predicted_V
    residual_window.append(abs(residual))
    mean_abs = sum(residual_window) / len(residual_window)

    # ---------- 5. auto re-seed if badly out of step at rest ----------
    if abs(residual) > RESEED_THRESHOLD_V and load_A < REST_CURRENT_A:
        bad_streak += 1
        if bad_streak >= RESEED_COUNT:
            soc = seed_from(measured_V, measured_T)
            print(f"  re-seeded: {measured_V:.3f} V -> SOC {soc*100:.1f}%")
            predicted_V = pack.pack_voltage
            residual = measured_V - predicted_V
            mean_abs = abs(residual)
    else:
        bad_streak = 0

    jumped = check_resistance_jump(client, now)
    if jumped:
        status = "resistance_jump"
    elif mean_abs >= RESIDUAL_ALARM_V:
        status = "divergent"
    elif mean_abs >= RESIDUAL_WARN_V:
        status = "drifting"
    else:
        status = "tracking"

    # ---------- 6. R0 from load steps, then feed it back into the model ----------
    step = load_A - prev_load
    if abs(step) > 0.3 and prev_voltage > 1.0:
        r0_pack = abs(prev_voltage - measured_V) / abs(step)
        r0_cell = r0_pack / N_CELLS
        if 0.005 < r0_cell < 0.5:
            r0_estimates.append(r0_cell)
            print(f"  R0 from step: {r0_cell*1000:.1f} mOhm per cell "
                  f"(model currently uses {PARAMS.R0_ohm*1000:.1f})")
    prev_load = load_A
    prev_voltage = measured_V

    r0_growth_pct = None
    if len(r0_estimates) >= R0_CALIBRATE_MIN_SAMPLES:
        r0_measured = sum(r0_estimates) / len(r0_estimates)

        # Move the model's R0 a fraction of the way towards what is
        # actually being measured, instead of jumping straight to it -
        # this is what stops the twin drifting once the pack ages.
        if abs(r0_measured - PARAMS.R0_ohm) > R0_CALIBRATE_DEADBAND_OHM:
            old = PARAMS.R0_ohm
            PARAMS.R0_ohm += (r0_measured - PARAMS.R0_ohm) * R0_CALIBRATE_STEP
            print(f"  model R0 recalibrated: {old*1000:.1f} -> "
                  f"{PARAMS.R0_ohm*1000:.1f} mOhm  "
                  f"(measured average {r0_measured*1000:.1f})")

        r0_growth_pct = (r0_measured - R0_BASELINE_OHM) / R0_BASELINE_OHM * 100.0

    out = {
        "status":          status,
        "predicted_V":     round(predicted_V, 4),
        "measured_V":      round(measured_V, 4),
        "residual_V":      round(residual, 4),
        "residual_mean":   round(mean_abs, 4),
        "model_soc":       round(pack.soc * 100.0, 2),
        "model_temp_C":    round(pack.max_temp, 2),
        "measured_temp_C": round(measured_T, 2),
        "model_R0_mOhm":   round(PARAMS.R0_ohm * 1000, 2),
        "ekf_soc":         round(ekf.soc * 100.0, 2),
        "ekf_soc_sigma":   round(ekf.sigma * 100.0, 2),
        "ekf_R0_mOhm":     round(ekf.r0 * 1000, 2),
        "ekf_innovation_V": round(ekf.innovation, 4),
    }
    if jumped:
        out.update({f"jump_{k}": v for k, v in jump_info.items()})
        if jump_info.get("action"):
            out["twin_action"] = jump_info["action"]
    if r0_estimates:
        out["R0_measured_mOhm"] = round(
            sum(r0_estimates) / len(r0_estimates) * 1000, 2)
    if r0_growth_pct is not None:
        out["R0_growth_pct"] = round(r0_growth_pct, 1)

    publish(client, out)

    if msg_count % 5 == 0:
        print(f"  model {predicted_V:6.3f} V | real {measured_V:6.3f} V | "
              f"residual {residual:+.3f} V | SOC {pack.soc*100:5.1f}% | "
              f"Kalman SOC {ekf.soc*100:5.1f}+/-{ekf.sigma*100:.1f}% | {status}")


def main():
    print("=" * 56)
    print("  Digital twin engine v4 - self-calibrating R0 + Kalman filter")
    print("=" * 56)
    print(f"  {N_CELLS} cells, {PARAMS.capacity_Ah} Ah, "
          f"starting R0 {PARAMS.R0_ohm*1000:.0f} mOhm "
          f"(will adjust to match measured load steps)")
    print(f"  plausible range {CELL_V_MIN_PLAUSIBLE}-{CELL_V_MAX_PLAUSIBLE} V/cell "
          f"({CELL_V_MIN_PLAUSIBLE*N_CELLS:.1f}-"
          f"{CELL_V_MAX_PLAUSIBLE*N_CELLS:.1f} V pack)\n")

    try:
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    except AttributeError:
        c = mqtt.Client()
    c.on_connect = on_connect
    c.on_message = on_message
    c.connect(MQTT_HOST, MQTT_PORT, 60)

    try:
        c.loop_forever()
    except KeyboardInterrupt:
        print("\nstopped.")


if __name__ == "__main__":
    main()
