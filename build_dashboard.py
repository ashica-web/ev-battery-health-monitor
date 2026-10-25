import json
import os

import dash_style

HERE = os.path.dirname(os.path.abspath(__file__))

B = "telemetry"
panels = []
pid = [0]


DS = {"type": "influxdb", "uid": "influxdb"}


def nid():
    pid[0] += 1
    return pid[0]


def q(measurement, fields, agg=True, last=False, fn="mean", keep_value=False):
    if isinstance(fields, str):
        fields = [fields]
    f = " or ".join([f'r["_field"] == "{x}"' for x in fields])
    s = (f'from(bucket: "{B}")\n'
         f'  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)\n'
         f'  |> filter(fn: (r) => r["_measurement"] == "{measurement}")\n'
         f'  |> filter(fn: (r) => {f})\n')
    if last:
        s += '  |> last()'
        if keep_value:
            s += '\n  |> keep(columns: ["_value"])'
    elif agg:
        s += f'  |> aggregateWindow(every: v.windowPeriod, fn: {fn}, createEmpty: false)'
    return [{"refId": "A", "datasource": DS, "query": s}]


def steps(*pairs):
    out = []
    for i, (c, v) in enumerate(pairs):
        out.append({"color": c, "value": None if i == 0 else v})
    return {"mode": "absolute", "steps": out}


def row(title, y):
    panels.append({"type": "row", "title": title, "id": nid(),
                   "collapsed": False,
                   "gridPos": {"h": 1, "w": 24, "x": 0, "y": y},
                   "panels": []})


def stat(title, x, y, w, h, targets, unit="short", dec=1, th=None,
         mappings=None, text=False, graph="area", color=None, novalue=None,
         size=None):
    fc = {"defaults": {"unit": unit, "decimals": dec}, "overrides": []}
    if novalue:
        fc["defaults"]["noValue"] = novalue
    if th:
        fc["defaults"]["thresholds"] = th
    if mappings:
        fc["defaults"]["mappings"] = mappings
    panels.append({
        "type": "stat", "title": title, "id": nid(), "datasource": DS,
        "gridPos": {"h": h, "w": w, "x": x, "y": y},
        "fieldConfig": fc,
        "options": {
            "colorMode": color or ("background" if mappings else "value"),
            "graphMode": "none" if text else graph,
            "textMode": "value",
            "justifyMode": "center",
            "text": {"titleSize": 13, **({"valueSize": size} if size else {})},
            "reduceOptions": {"calcs": ["lastNotNull"],
                              "fields": "/.*/" if text else "",
                              "values": False},
        },
        "targets": targets,
    })


def ts(title, x, y, w, h, targets, unit="short", dec=2, th=None,
       fill=10, step=False, thresh_style=None, overrides=None,
       minv=None, maxv=None):
    d = {"unit": unit, "decimals": dec,
         "custom": {"lineWidth": 2, "fillOpacity": fill,
                    "showPoints": "never", "spanNulls": True,
                    "gradientMode": "opacity"}}
    if step:
        d["custom"]["lineInterpolation"] = "stepAfter"
    if th:
        d["thresholds"] = th
    if thresh_style:
        d["custom"]["thresholdsStyle"] = {"mode": thresh_style}
    if minv is not None:
        d["min"] = minv
    if maxv is not None:
        d["max"] = maxv
    panels.append({
        "type": "timeseries", "title": title, "id": nid(), "datasource": DS,
        "gridPos": {"h": h, "w": w, "x": x, "y": y},
        "fieldConfig": {"defaults": d, "overrides": overrides or []},
        "options": {"legend": {"displayMode": "list", "placement": "bottom"},
                    "tooltip": {"mode": "multi", "sort": "none"}},
        "targets": targets,
    })


def gauge(title, x, y, w, h, targets, unit, mn, mx, th, dec=1):
    panels.append({
        "type": "gauge", "title": title, "id": nid(), "datasource": DS,
        "gridPos": {"h": h, "w": w, "x": x, "y": y},
        "fieldConfig": {"defaults": {"unit": unit, "min": mn, "max": mx,
                                     "decimals": dec, "thresholds": th},
                        "overrides": []},
        "options": {"reduceOptions": {"calcs": ["lastNotNull"]},
                    "showThresholdMarkers": True},
        "targets": targets,
    })


def html(title, x, y, w, h, content):
    panels.append({
        "type": "text", "title": title, "id": nid(),
        "gridPos": {"h": h, "w": w, "x": x, "y": y},
        "options": {"mode": "html", "content": content},
    })


def rename(frm, to, color=None):
    props = [{"id": "displayName", "value": to}]
    if color:
        props.append({"id": "color",
                      "value": {"mode": "fixed", "fixedColor": color}})
    return {"matcher": {"id": "byRegexp", "options": f".*{frm}.*"},
            "properties": props}


def flux_one(measurement, field, fn="mean"):
    return (f'from(bucket: "{B}")\n'
            f'  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)\n'
            f'  |> filter(fn: (r) => r["_measurement"] == "{measurement}")\n'
            f'  |> filter(fn: (r) => r["_field"] == "{field}")\n'
            f'  |> aggregateWindow(every: v.windowPeriod, fn: {fn}, createEmpty: false)')


def shap_bars(title, x, y, w, h):
    panels.append({
        "type": "bargauge", "title": title, "id": nid(), "datasource": DS,
        "gridPos": {"h": h, "w": w, "x": x, "y": y},
        "description": "SHAP values: health points each input moved the estimate "
                       "away from a new battery. Red pulls health down.",
        "fieldConfig": {"defaults": {"unit": "none", "decimals": 1, "min": -20, "max": 5,
                                     "thresholds": steps(("red", 0), ("green", 0))},
                        "overrides": [rename("shap_voltage", "Discharge voltage"),
                                      rename("shap_temperature", "Cell temperature"),
                                      rename("shap_cycle", "Charge cycles")]},
        "options": {"orientation": "horizontal", "displayMode": "gradient",
                    "showUnfilled": True, "valueMode": "color",
                    "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}},
        "targets": q("ai", ["shap_voltage", "shap_temperature", "shap_cycle"], last=True),
    })


def reasons_table(title, x, y, w, h):
    s = (f'from(bucket: "{B}")\n'
         '  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)\n'
         '  |> filter(fn: (r) => r["_measurement"] == "ai")\n'
         '  |> filter(fn: (r) => r["_field"] == "reason1" or r["_field"] == "reason2"'
         ' or r["_field"] == "reason3" or r["_field"] == "evidence"'
         ' or r["_field"] == "input_warning")\n'
         '  |> last()\n  |> group()\n'
         '  |> map(fn: (r) => ({r with order: if r._field == "input_warning" then "0" else if r._field == "evidence" then "z" else r._field}))\n'
         '  |> sort(columns: ["order"])\n'
         '  |> keep(columns: ["_field", "_value"])')
    panels.append({
        "type": "table", "title": title, "id": nid(), "datasource": DS,
        "gridPos": {"h": h, "w": w, "x": x, "y": y},
        "fieldConfig": {"defaults": {"custom": {"align": "left"}},
                        "overrides": [
            {"matcher": {"id": "byName", "options": "_field"},
             "properties": [{"id": "displayName", "value": " "},
                            {"id": "custom.width", "value": 110},
                            {"id": "mappings", "value": [{"type": "value", "options": {
                                "reason1": {"text": "Biggest", "index": 0},
                                "reason2": {"text": "Second", "index": 1},
                                "reason3": {"text": "Third", "index": 2},
                                "evidence": {"text": "Measured", "index": 3},
                                "input_warning": {"text": "CAUTION", "color": "orange", "index": 4}}}]}]},
            {"matcher": {"id": "byName", "options": "_value"},
             "properties": [{"id": "displayName", "value": "Why the AI gave this estimate"}]}]},
        "options": {"showHeader": True, "cellHeight": "md"},
        "targets": [{"refId": "A", "datasource": DS, "query": s}],
    })


def table(title, x, y, w, h, query, overrides=None, desc=None):
    p = {"type": "table", "title": title, "id": nid(), "datasource": DS,
         "gridPos": {"h": h, "w": w, "x": x, "y": y},
         "fieldConfig": {"defaults": {"custom": {"align": "left"}},
                         "overrides": overrides or []},
         "options": {"showHeader": True, "cellHeight": "sm"},
         "targets": [{"refId": "A", "datasource": DS, "query": query}]}
    if desc:
        p["description"] = desc
    panels.append(p)


def col(name, label, unit=None, width=None, dec=None):
    props = [{"id": "displayName", "value": label}]
    if unit:
        props.append({"id": "unit", "value": unit})
    if width:
        props.append({"id": "custom.width", "value": width})
    if dec is not None:
        props.append({"id": "decimals", "value": dec})
    return {"matcher": {"id": "byName", "options": name}, "properties": props}


MODE_LINKS = [{"title": "Modes", "type": "dashboards", "tags": ["battery"],
               "asDropdown": False, "includeVars": False, "keepTime": True,
               "targetBlank": False, "icon": "dashboard"}]

VERDICT = [{"type": "value", "options": {
    "Normal":   {"text": "NORMAL",   "color": "green",  "index": 0},
    "Warning":  {"text": "WARNING",  "color": "orange", "index": 1},
    "Critical": {"text": "CRITICAL", "color": "red",    "index": 2}}}]


# ============ TOP: STATUS STRIP + KEY READINGS ============
y = 0
TWIN_STATUS = [{"type": "value", "options": {
    "tracking":  {"text": "TRACKING",  "color": "green",  "index": 0},
    "drifting":  {"text": "DRIFTING",  "color": "orange", "index": 1},
    "divergent": {"text": "DIVERGENT", "color": "red",    "index": 2},
    "no_data":   {"text": "NO VOLTAGE", "color": "text",  "index": 3},
    "implausible": {"text": "CHECK WIRING", "color": "purple", "index": 4},
    "replay":    {"text": "LAB REPLAY", "color": "blue",  "index": 5},
    "resistance_jump": {"text": "RESISTANCE JUMP", "color": "red", "index": 6},
}}]
SAFETY = [{"type": "value", "options": {
    "0": {"text": "ALL CLEAR", "color": "green", "index": 0},
    "1": {"text": "CUTOFF ACTIVE", "color": "red", "index": 1}}}]
AGE = (f'from(bucket: "{B}")\n'
       '  |> range(start: -24h)\n'
       '  |> filter(fn: (r) => r["_measurement"] == "battery" and r["_field"] == "pack_voltage")\n'
       '  |> last()\n'
       '  |> map(fn: (r) => ({_time: r._time, _value: float(v: int(v: now()) - int(v: r._time)) / 1000000000.0}))')

stat("ESP32 safety", 0, y, 6, 3, q("battery", "safety_active", last=True, keep_value=True),
     text=True, mappings=SAFETY, novalue="—", size=26)
stat("Digital twin", 6, y, 6, 3, q("twin", "status", last=True, keep_value=True),
     text=True, mappings=TWIN_STATUS, novalue="—", size=26)
stat("AI verdict", 12, y, 6, 3, q("ai", "prediction", last=True, keep_value=True),
     text=True, mappings=VERDICT, novalue="LEARNING", size=26)
stat("Last reading (seconds ago)", 18, y, 6, 3,
     [{"refId": "A", "datasource": DS, "query": AGE}], "s", 0,
     steps(("green", 0), ("orange", 15), ("red", 60)), graph="none",
     color="background", novalue="NO DATA", size=26)
y += 3

GRAD = "value"
# A sensor that is not wired yet reads 0 - say so instead of showing a red 0.
def not_connected(below):
    return [{"type": "range", "options": {"from": -1000, "to": below,
             "result": {"text": "Not connected", "color": "text", "index": 0}}}]

stat("State of charge (Kalman)", 0, y, 3, 4, q("twin", "ekf_soc", last=True),
     "percent", 1, steps(("red", 0), ("orange", 20), ("green", 50)), color=GRAD, graph="none",
     novalue="Needs voltage")
stat("AI health estimate", 3, y, 3, 4, q("ai", "est_soh", last=True), "percent", 1,
     steps(("red", 0), ("orange", 80), ("yellow", 85), ("green", 90)), color=GRAD, graph="none",
     novalue="—")
stat("Pack voltage", 6, y, 3, 4, q("battery", "pack_voltage", last=True),
     "volt", 2, steps(("red", 0), ("orange", 9.0), ("green", 10.5)), color=GRAD, graph="none",
     mappings=not_connected(0.5))
stat("Current", 9, y, 3, 4, q("battery", "current_A", last=True),
     "amp", 2, steps(("blue", None)), color=GRAD, graph="none")
stat("Power", 12, y, 3, 4, q("battery", "power_W", last=True),
     "watt", 1, steps(("blue", None)), color=GRAD, graph="none")
stat("Hottest cell", 15, y, 3, 4, [{"refId": "A", "datasource": DS, "query":
     f'from(bucket: "{B}")\n'
     '  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)\n'
     '  |> filter(fn: (r) => r["_measurement"] == "battery")\n'
     '  |> filter(fn: (r) => r["_field"] == "cell1_temp" or '
     'r["_field"] == "cell2_temp" or r["_field"] == "cell3_temp")\n'
     '  |> last()\n  |> group()\n  |> max()\n'
     '  |> keep(columns: ["_value"])'}],
     "celsius", 1, steps(("green", 0), ("orange", 45), ("red", 55)), color=GRAD, graph="none")
stat("Cell imbalance", 18, y, 3, 4, q("battery", "cell_imbalance", last=True),
     "volt", 3, steps(("green", 0), ("orange", 0.05), ("red", 0.1)), color=GRAD, graph="none",
     novalue="—")
stat("Gas level", 21, y, 3, 4, q("battery", "gas_level", last=True),
     "short", 0, steps(("green", 0), ("orange", 1500), ("red", 2500)), color=GRAD, graph="none",
     mappings=not_connected(0.5))
y += 4


# ============ ROW 2: DIGITAL TWIN ============
row("Digital twin — physics model vs physical pack", y); y += 1

ts("Voltage: model prediction vs measured", 0, y, 12, 8,
   q("twin", ["predicted_V", "measured_V"]), "volt", 3, fill=5,
   overrides=[rename("predicted_V", "Model (predicted)", "#8ab8ff"),
              rename("measured_V", "Physical (measured)", "#73bf69")])

ts("Residual (measured − predicted)", 12, y, 8, 8,
   q("twin", ["residual_V"]), "volt", 3, fill=20,
   th=steps(("green", 0), ("orange", 0.15), ("red", 0.35)),
   thresh_style="line+area",
   overrides=[rename("residual_V", "Residual")])

gauge("Mean |residual| (60 s)", 20, y, 4, 8,
      q("twin", "residual_mean", last=True), "volt", 0, 0.5,
      steps(("green", 0), ("orange", 0.15), ("red", 0.35)), dec=3)
y += 8

ts("Temperature: model vs measured", 0, y, 12, 7,
   q("twin", ["model_temp_C", "measured_temp_C"]), "celsius", 2, fill=5,
   overrides=[rename("model_temp_C", "Model", "#8ab8ff"),
              rename("measured_temp_C", "Physical", "#ff9830")])

stat("Internal resistance R0", 12, y, 4, 7,
     q("twin", "R0_measured_mOhm", last=True), "none", 1,
     steps(("green", 0), ("orange", 60), ("red", 90)), graph="area")
stat("R0 growth vs new cell", 16, y, 4, 7,
     q("twin", "R0_growth_pct", last=True), "percent", 1,
     steps(("green", 0), ("orange", 30), ("red", 60)), graph="area")
stat("Model SOC", 20, y, 4, 7,
     q("twin", "model_soc", last=True), "percent", 1,
     steps(("red", 0), ("orange", 20), ("green", 50)))
y += 7

# ---- Kalman filter: state estimation ----
ts("State of charge - Kalman filter vs coulomb counting (firmware)", 0, y, 12, 8,
   [{"refId": "A", "datasource": DS, "query": flux_one("twin", "ekf_soc")},
    {"refId": "B", "datasource": DS, "query": flux_one("battery", "soc_percent")},
    {"refId": "C", "datasource": DS, "query": flux_one("battery", "sim_true_soc")}],
   "percent", 1, fill=0, minv=0, maxv=100,
   overrides=[rename("ekf_soc", "Kalman filter", "#ff9830"),
              rename("soc_percent", "Coulomb counting (firmware)", "#8e8e8e"),
              rename("sim_true_soc", "True SOC (simulation only)", "#5794f2")])
ts("Internal resistance learned by the Kalman filter (jump = loose connection)",
   12, y, 8, 8, q("twin", ["ekf_R0_mOhm"]), "none", 1, fill=12,
   overrides=[rename("ekf_R0_mOhm", "R0 per cell (mOhm)", "#f2495c")])
stat("Kalman SOC uncertainty (±1σ)", 20, y, 4, 8, q("twin", "ekf_soc_sigma", last=True),
     "percent", 2, steps(("green", 0), ("orange", 3), ("red", 6)))
y += 8

# ---- two-way control: the twin acting on the real pack ----
row("Control — load, fan, and the twin acting on the pack", y); y += 1
stat("Current limit set by the twin", 0, y, 5, 6, q("battery", "current_limit_A", last=True),
     "amp", 2, steps(("red", 0), ("green", 4.99)),
     mappings=[{"type": "range", "options": {"from": 4.99, "to": 1000,
                                             "result": {"text": "NO LIMIT", "color": "green"}}}])
gauge("Cooling fan (PWM 0-255)", 5, y, 5, 6, q("battery", "fan_duty", last=True),
      "none", 0, 255, steps(("blue", 0), ("green", 60), ("orange", 200)), dec=0)
ts("Load: requested vs the twin's cap", 0, y + 6, 10, 9,
   q("battery", ["target_current", "current_limit_A"], fn="last"), "amp", 2, fill=8,
   step=True, minv=0, maxv=3.2,
   overrides=[rename("target_current", "Requested load", "#8ab8ff"),
              rename("current_limit_A", "Twin's limit", "#f2495c")])
html("Load & fan control", 10, y, 14, 15,
     '<iframe src="http://localhost:8503/?embed=true" style="width:100%;height:100%;'
     'border:0;border-radius:4px;background:#181b1f"></iframe>')
y += 15


# ============ ROW 2b: 3D TWIN (Unity WebGL) ============
row("3D digital twin", y); y += 1
html("Live 3D model", 0, y, 24, 16,
     '<iframe src="http://localhost:8504/?embed=true" '
     'style="width:100%;height:100%;border:0;border-radius:4px;'
     'background:#0b0e14" allow="fullscreen"></iframe>')
y += 16


# ============ ROW 3: AI ============
row("AI — health estimate without a capacity test (XGBoost + SHAP)", y); y += 1

stat("AI health estimate", 0, y, 4, 8, q("ai", "est_soh", last=True), "percent", 1,
     steps(("red", 0), ("orange", 80), ("yellow", 85), ("green", 90)))
stat("Cycles left until 80%", 4, y, 4, 8, q("ai", "rul_cycles", last=True), "none", 0,
     steps(("red", 0), ("orange", 10), ("yellow", 25), ("green", 50)),
     mappings=[{"type": "value", "options": {"-1": {"text": "learning trend", "index": 0}}}])
stat("On ESP32 chip", 8, y, 4, 8, q("ai_edge", "est_soh", last=True), "percent", 1,
     steps(("red", 0), ("orange", 80), ("yellow", 85), ("green", 90)))
ts("AI estimate vs measured health", 12, y, 12, 8,
   [{"refId": "A", "datasource": DS, "query": flux_one("battery", "soh_percent")},
    {"refId": "B", "datasource": DS, "query": flux_one("ai", "est_soh", fn="last")},
    {"refId": "C", "datasource": DS, "query": flux_one("ai_edge", "est_soh", fn="last")}],
   "percent", 1, fill=0, minv=60, maxv=102,
   th=steps(("red", 0), ("orange", 80), ("yellow", 85), ("green", 90)),
   thresh_style="line",
   overrides=[rename("soh_percent", "Measured (capacity test)", "#5794f2"),
              {"matcher": {"id": "byFrameRefID", "options": "B"},
               "properties": [{"id": "displayName", "value": "AI (laptop)"},
                              {"id": "color", "value": {"mode": "fixed", "fixedColor": "#ff9830"}}]},
              {"matcher": {"id": "byFrameRefID", "options": "C"},
               "properties": [{"id": "displayName", "value": "AI (ESP32 chip)"},
                              {"id": "color", "value": {"mode": "fixed", "fixedColor": "#b877d9"}},
                              {"id": "custom.lineStyle", "value": {"fill": "dash", "dash": [8, 6]}}]}])
y += 8
shap_bars("Why: health points lost vs a new battery (SHAP)", 0, y, 10, 7)
reasons_table("In plain words", 10, y, 14, 7)
y += 7


# ============ ROW 4: CELLS ============
row("Cells", y); y += 1

ts("Cell temperatures", 0, y, 12, 8,
   q("battery", ["cell1_temp", "cell2_temp", "cell3_temp", "ambient_temp"]),
   "celsius", 2, fill=6,
   th=steps(("green", 0), ("orange", 45), ("red", 55)), thresh_style="line")
ts("Cell voltages", 12, y, 12, 8,
   q("battery", ["cell1_voltage", "cell2_voltage", "cell3_voltage"]),
   "volt", 3, fill=6,
   th=steps(("red", 0), ("orange", 3.0), ("green", 3.3),
            ("orange", 4.15), ("red", 4.25)))
y += 8

ts("Current & power", 0, y, 8, 7,
   q("battery", ["current_A", "power_W"]), "short", 2)
ts("SOC / SOH", 8, y, 8, 7,
   q("battery", ["soc_percent", "soh_percent"]), "percent", 1,
   minv=0, maxv=105)
ts("Imbalance & thermal gradient", 16, y, 8, 7,
   q("battery", ["cell_imbalance", "temp_gradient"]), "short", 3)
y += 7


# ============ ROW 5: SAFETY ============
row("Safety", y); y += 1

ts("Gas level", 0, y, 8, 6, q("battery", "gas_level"), "short", 0, fill=15,
   th=steps(("green", 0), ("orange", 1500), ("red", 2500)),
   thresh_style="line")
ts("dT/dt (runaway precursor)", 8, y, 8, 6, q("battery", "dT_dt"),
   "short", 3, fill=15,
   th=steps(("green", 0), ("orange", 0.5), ("red", 1.0)),
   thresh_style="line")
ts("Swelling sensors", 16, y, 8, 6,
   q("battery", ["piezo1_raw", "piezo2_raw"]), "short", 0)
y += 6

ts("Fault flags", 0, y, 24, 5,
   q("battery", ["fault_ov", "fault_uv", "fault_ot", "fault_gas",
                 "bms_tripped"], fn="max"),
   "short", 0, fill=30, step=True, minv=0, maxv=1.2)
y += 5


# ============ DATA LOG: every reading with its time ============
row("Data log — every reading with its timestamp", y); y += 1

LAST_TIME = (f'from(bucket: "{B}")\n'
             '  |> range(start: -24h)\n'
             '  |> filter(fn: (r) => r["_measurement"] == "battery" and r["_field"] == "pack_voltage")\n'
             '  |> last()\n'
             '  |> map(fn: (r) => ({_time: r._time, _value: float(v: uint(v: r._time)) / 1000000.0}))')
stat("Last reading received at", 0, y, 6, 5, [{"refId": "A", "datasource": DS, "query": LAST_TIME}],
     "dateTimeAsLocalNoDateIfToday", 0, steps(("text", 0)))
stat("Data age", 0, y + 5, 6, 5, [{"refId": "A", "datasource": DS, "query": LAST_TIME}],
     "dateTimeFromNow", 0, steps(("text", 0)))

READINGS = ["pack_voltage", "cell1_voltage", "cell2_voltage", "cell3_voltage",
            "current_A", "power_W", "soc_percent", "soh_percent",
            "cell1_temp", "cell2_temp", "cell3_temp", "ambient_temp", "ambient_humidity",
            "gas_level", "piezo1_raw", "piezo2_raw", "fan_duty", "target_current",
            "current_limit_A", "internal_resistance_mohm", "thermal_runaway_risk",
            "cell_imbalance", "dT_dt"]
filt = " or ".join(f'r["_field"] == "{f}"' for f in READINGS)
table("Latest value of every sensor reading", 6, y, 8, 18,
      f'from(bucket: "{B}")\n'
      '  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)\n'
      '  |> filter(fn: (r) => r["_measurement"] == "battery")\n'
      f'  |> filter(fn: (r) => {filt})\n'
      '  |> last()\n  |> group()\n'
      '  |> keep(columns: ["_time", "_field", "_value"])\n'
      '  |> sort(columns: ["_field"])',
      overrides=[col("_time", "Time", "dateTimeAsLocal", 165),
                 col("_field", "Reading", width=170), col("_value", "Value", dec=3)])

table("AI results log (one row per discharge)", 14, y, 10, 9,
      f'from(bucket: "{B}")\n'
      '  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)\n'
      '  |> filter(fn: (r) => r["_measurement"] == "ai")\n'
      '  |> filter(fn: (r) => r["_field"] == "prediction" or r["_field"] == "est_soh"'
      ' or r["_field"] == "rul_cycles" or r["_field"] == "cycle" or r["_field"] == "kind")\n'
      '  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")\n'
      '  |> filter(fn: (r) => r.kind == "cycle")\n'
      '  |> group()\n'
      '  |> keep(columns: ["_time", "cycle", "est_soh", "prediction", "rul_cycles"])\n'
      '  |> sort(columns: ["_time"], desc: true)\n  |> limit(n: 50)',
      overrides=[col("_time", "Time", "dateTimeAsLocal", 165), col("cycle", "Cycle", dec=0),
                 col("est_soh", "AI health %", dec=1), col("prediction", "Verdict"),
                 col("rul_cycles", "Cycles to 80%", dec=0)])

table("Event log (safety, resistance jumps, twin actions)", 14, y + 9, 10, 9,
      f'from(bucket: "{B}")\n'
      '  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)\n'
      '  |> filter(fn: (r) => r["_measurement"] == "event")\n'
      '  |> filter(fn: (r) => r["_field"] == "event" or r["_field"] == "action"'
      ' or r["_field"] == "reason")\n'
      '  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")\n'
      '  |> group()\n'
      '  |> keep(columns: ["_time", "event", "action", "reason"])\n'
      '  |> sort(columns: ["_time"], desc: true)\n  |> limit(n: 50)',
      overrides=[col("_time", "Time", "dateTimeAsLocal", 165), col("event", "Event", width=170),
                 col("action", "Twin action"), col("reason", "Reason")])
html("Every reading is also saved", 0, y + 10, 6, 8, """
<div style="font-size:13px;line-height:1.6;padding:4px 8px;color:#ccccdc">
Every sensor reading is stored with its timestamp in InfluxDB and in a daily
CSV file (<code>data/battery_YYYY-MM-DD.csv</code>). Hover over any chart to
see the exact time of each point. Change the time range at the top right to
look back further.</div>""")
y += 18

html("How to read this dashboard", 0, y, 24, 9, """
<div style="font-size:13px;line-height:1.6;padding:4px 6px;color:#ccccdc">
<b>Top strip</b> – four answers at a glance: is the ESP32's safety cutoff
on, does the twin agree with the pack, what does the AI say, and is data
still arriving. Below it, the key live readings.<br><br>
<b>Digital twin</b> – a Thevenin equivalent-circuit model runs in
parallel, fed the same current as the real pack. The residual is the
gap between prediction and measurement. A healthy pack tracks its model;
a degrading one diverges.<br><br>
<b>R0</b> – internal resistance, estimated each time the load changes.
Rising R0 is the strongest single indicator of ageing.<br><br>
<b>Kalman filter</b> – every second it predicts SOC from the current, then
corrects it with the measured voltage, learning R0 as it goes. It fixes the
drift of coulomb counting and keeps the twin in sync. A sudden R0 rise
raises a RESISTANCE JUMP alarm (loose connection or damaged cell).<br><br>
<b>AI</b> – an XGBoost model estimates state of health from discharge
voltage, temperature and cycle count, without a capacity test. SHAP shows
which input moved the estimate. Warning = estimate below 85% or projected
to reach 80% within 25 cycles; Critical = below 80%.<br><br>
<b>Tabs</b> – Live pack (this page), AI health demo (an unseen NASA battery
replayed through the AI) and What-if simulator (conditions you would never
risk on real cells).
</div>""")


dash = {
    "title": "1 · Live Digital Twin",
    "uid": "ev-battery-twin",
    "tags": ["battery", "digital-twin"],
    "links": MODE_LINKS,
    "editable": True,
    "refresh": "5s",
    "time": {"from": "now-30m", "to": "now"},
    "schemaVersion": 39,
    "version": 1,
    "panels": panels,
}

# ---- hover help (the (i) next to a panel title) ----
HELP = {
    "ESP32 safety": "The ESP32's own hard limits (over/under-voltage, over-temperature). "
                    "If tripped it switches the load off by itself - no laptop needed.",
    "Digital twin": "Does the physics model still agree with the real pack? "
                    "TRACKING = yes. DRIFTING/DIVERGENT = the pack is behaving unlike a healthy one.",
    "AI verdict": "XGBoost health estimate after each discharge. Warning = below 85% or "
                  "projected to reach 80% within 25 cycles. Critical = below 80%.",
    "Last reading (seconds ago)": "Time since the last sensor reading arrived. Green = live.",
    "State of charge (Kalman)": "Charge left, from the Kalman filter: current-based prediction "
                                "corrected every second by the measured voltage.",
    "AI health estimate": "State of health estimated without a capacity test.",
    "Cell imbalance": "Highest minus lowest cell voltage. Above 0.1 V the pack needs balancing.",
    "Residual (measured − predicted)": "Gap between the real pack and its digital twin. "
                                       "Growing gap = something has changed in the pack.",
    "Why: health points lost vs a new battery (SHAP)": "SHAP: how many health points each "
                                       "input cost compared with a new battery.",
}
for pnl in panels:
    if pnl.get("title") in HELP:
        pnl["description"] = HELP[pnl["title"]]

dash_style.add_header(panels, dash["uid"])
dash_style.finish(dash)

prov = os.path.join(HERE, "grafana", "provisioning", "dashboards")
path = os.path.join(prov if os.path.isdir(prov) else HERE, "ev-battery-digital-twin.json")
with open(path, "w") as f:
    json.dump(dash, f, indent=2)

print(f"{len(panels)} panels written -> {os.path.relpath(path, HERE)}")
