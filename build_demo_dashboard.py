"""
Builds the "EV Battery - AI Health Demo" Grafana dashboard.

One job: show judges the AI estimating the health of a battery it has
never seen, next to the health measured in the lab, with the reasons.

    estimate vs lab truth  ->  why (SHAP)  ->  cycles left  ->  verdict

Run alongside demo_nasa_replay.py. train_health_model.py reruns this
automatically so the talking points quote your actual validation numbers.
"""
import json
import os

import dash_style

HERE = os.path.dirname(os.path.abspath(__file__))
B = "telemetry"
DS = {"type": "influxdb", "uid": "influxdb"}
panels = []
pid = [0]


def load_meta():
    try:
        with open(os.path.join(HERE, "health_model_meta.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def out_dir():
    prov = os.path.join(HERE, "grafana", "provisioning", "dashboards")
    return prov if os.path.isdir(prov) else HERE


def nid():
    pid[0] += 1
    return pid[0]


def flux(measurement, fields, last=False, fn="mean", keep_value=False):
    if isinstance(fields, str):
        fields = [fields]
    filt = " or ".join([f'r["_field"] == "{x}"' for x in fields])
    s = (f'from(bucket: "{B}")\n'
         f'  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)\n'
         f'  |> filter(fn: (r) => r["_measurement"] == "{measurement}")\n'
         f'  |> filter(fn: (r) => {filt})\n')
    if last:
        s += '  |> last()'
        if keep_value:
            s += '\n  |> keep(columns: ["_value"])'
    else:
        s += f'  |> aggregateWindow(every: v.windowPeriod, fn: {fn}, createEmpty: false)'
    return s


def q(measurement, fields, **kw):
    return [{"refId": "A", "datasource": DS, "query": flux(measurement, fields, **kw)}]


def steps(*pairs):
    return {"mode": "absolute",
            "steps": [{"color": c, "value": None if i == 0 else v}
                      for i, (c, v) in enumerate(pairs)]}


def row(title, y):
    panels.append({"type": "row", "title": title, "id": nid(), "collapsed": False,
                   "gridPos": {"h": 1, "w": 24, "x": 0, "y": y}, "panels": []})


def stat(title, x, y, w, h, targets, unit="short", dec=1, th=None,
         mappings=None, text=False, graph="none", size=None, desc=None):
    fc = {"defaults": {"unit": unit, "decimals": dec}, "overrides": []}
    if th:
        fc["defaults"]["thresholds"] = th
    if mappings:
        fc["defaults"]["mappings"] = mappings
    opts = {"colorMode": "background" if (mappings and text) else "value",
            "graphMode": "none" if text else graph, "textMode": "value",
            "justifyMode": "center",
            "reduceOptions": {"calcs": ["lastNotNull"],
                              "fields": "/.*/" if text else "", "values": False}}
    if size:
        opts["text"] = {"valueSize": size}
    p = {"type": "stat", "title": title, "id": nid(), "datasource": DS,
         "gridPos": {"h": h, "w": w, "x": x, "y": y},
         "fieldConfig": fc, "options": opts, "targets": targets}
    if desc:
        p["description"] = desc
    panels.append(p)


def ts(title, x, y, w, h, targets, unit="short", dec=2, th=None, fill=12,
       thresh_style=None, overrides=None, minv=None, maxv=None, step=False):
    d = {"unit": unit, "decimals": dec,
         "custom": {"lineWidth": 3, "fillOpacity": fill, "showPoints": "never",
                    "spanNulls": True, "gradientMode": "opacity"}}
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
    panels.append({"type": "timeseries", "title": title, "id": nid(), "datasource": DS,
                   "gridPos": {"h": h, "w": w, "x": x, "y": y},
                   "fieldConfig": {"defaults": d, "overrides": overrides or []},
                   "options": {"legend": {"displayMode": "list", "placement": "bottom"},
                               "tooltip": {"mode": "multi"}},
                   "targets": targets})


def rename(frm, to, color=None):
    props = [{"id": "displayName", "value": to}]
    if color:
        props.append({"id": "color", "value": {"mode": "fixed", "fixedColor": color}})
    return {"matcher": {"id": "byRegexp", "options": f".*{frm}.*"}, "properties": props}


def html(title, x, y, w, h, content):
    panels.append({"type": "text", "title": title, "id": nid(),
                   "gridPos": {"h": h, "w": w, "x": x, "y": y},
                   "options": {"mode": "html", "content": content}})


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


VERDICT = [{"type": "value", "options": {
    "Normal":   {"text": "NORMAL",   "color": "green",  "index": 0},
    "Warning":  {"text": "WARNING",  "color": "orange", "index": 1},
    "Critical": {"text": "CRITICAL", "color": "red",    "index": 2}}}]
RUL_MAP = [{"type": "value", "options": {"-1": {"text": "learning trend", "index": 0}}}]
HEALTH_TH = steps(("red", 0), ("orange", 80), ("yellow", 85), ("green", 90))


# ================= THE STORY =================
meta = load_meta()
y = 0

row("Is this battery in trouble?", y); y += 1
stat("AI health estimate (no capacity test)", 0, y, 6, 6,
     q("ai", "est_soh", last=True), "percent", 1, HEALTH_TH, size=60,
     desc="Estimated from discharge voltage, temperature and cycle count only.")
stat("Lab-measured health", 6, y, 5, 6, q("battery", "soh_percent", last=True),
     "percent", 1, HEALTH_TH, size=52,
     desc="From a full capacity test - the ground truth the AI never sees.")
stat("AI verdict", 11, y, 5, 6, q("ai", "prediction", last=True, keep_value=True),
     text=True, mappings=VERDICT)
stat("Cycles left until 80% (AI)", 16, y, 5, 6, q("ai", "rul_cycles", last=True),
     "none", 0, steps(("red", 0), ("orange", 10), ("yellow", 25), ("green", 50)),
     mappings=RUL_MAP, size=52)
stat("Cycle", 21, y, 3, 6, q("ai", "cycle", last=True), "none", 0,
     steps(("text", 0)))
y += 6

row("Does the AI match the lab?", y); y += 1
ts("AI estimate vs lab-measured health  (85% = service line, 80% = end of life)",
   0, y, 24, 10,
   [{"refId": "A", "datasource": DS, "query": flux("battery", "soh_percent")},
    {"refId": "B", "datasource": DS, "query": flux("ai", "est_soh", fn="last")},
    {"refId": "C", "datasource": DS, "query": flux("ai_edge", "est_soh", fn="last")}],
   "percent", 1, fill=0, th=HEALTH_TH, thresh_style="line", minv=60, maxv=102,
   overrides=[rename("soh_percent", "Lab-measured health", "#5794f2"),
              {"matcher": {"id": "byFrameRefID", "options": "B"},
               "properties": [{"id": "displayName", "value": "AI estimate (laptop)"},
                              {"id": "color", "value": {"mode": "fixed", "fixedColor": "#ff9830"}}]},
              {"matcher": {"id": "byFrameRefID", "options": "C"},
               "properties": [{"id": "displayName", "value": "AI estimate (on ESP32 chip)"},
                              {"id": "color", "value": {"mode": "fixed", "fixedColor": "#b877d9"}},
                              {"id": "custom.lineStyle", "value": {"fill": "dash", "dash": [8, 6]}}]}])
y += 10

row("The same AI, running on the ESP32 chip", y); y += 1
stat("On-chip AI health estimate", 0, y, 6, 6, q("ai_edge", "est_soh", last=True),
     "percent", 1, HEALTH_TH, size=52,
     desc="Computed by the ESP32 itself - no laptop, no cloud.")
stat("Time for one estimate on the chip", 6, y, 6, 6, q("ai_edge", "infer_us", last=True),
     "µs", 0, steps(("green", 0)), size=52)
stat("Time to explain it (SHAP) on the chip", 12, y, 6, 6,
     q("ai_edge", "explain_ms", last=True), "ms", 0, steps(("green", 0)), size=52)
html("Why this matters", 18, y, 6, 6, """
<div style="font-size:13.5px;line-height:1.6;padding:4px 8px;color:#ccccdc">
A car's battery system cannot depend on a laptop or the cloud. The trained
model is compiled into the ESP32's firmware, so the chip estimates health
<b>and explains it</b> on its own, in the time shown here. The dashed purple
line above is the chip; it lands on the laptop's line because it is the
same model.</div>""")
y += 6

row("Why - what drove the estimate (explainable AI)", y); y += 1
shap_bars("Health points lost or gained, versus a new battery", 0, y, 10, 8)
reasons_table("In plain words", 10, y, 14, 8)
y += 8

row("The digital twin, live", y); y += 1
html("3D model", 0, y, 24, 13,
     '<iframe src="http://localhost:8504/?embed=true" '
     'style="width:100%;height:100%;border:0;border-radius:4px;'
     'background:#181b1f" allow="fullscreen"></iframe>')
y += 13

if meta:
    lead = f"{meta['median_lead']:.0f}" if meta.get("median_lead") is not None else "n/a"
    numbers = (f"Tested on <b>{meta['n_batteries']} batteries it never saw</b> "
               f"(leave-one-battery-out): average error <b>{meta['mae']:.1f} health points</b>, "
               f"90% of estimates within &plusmn;{meta['p90']:.1f}. Counting cycles alone "
               f"gives {meta['mae_baseline']:.1f} points, so the AI cuts the error by "
               f"{(1 - meta['mae'] / meta['mae_baseline']) * 100:.0f}%. "
               f"It warned before end of life on {meta['early_warned']} of "
               f"{meta['reached_eol']} batteries, median <b>{lead} cycles early</b>, "
               f"with {meta['false_alarms']} false alarms above 90% health.")
    demo_b = meta.get("demo_battery", "B0005")
    model_name = meta.get("backend", "XGBoost")
else:
    numbers = "Run <code>python train_health_model.py</code> to fill in the validation numbers."
    demo_b, model_name = "B0005", "XGBoost"

row("What this demo shows", y); y += 1
html("Talking points", 0, y, 24, 9, f"""
<div style="font-size:14px;line-height:1.75;padding:6px 10px;color:#ccccdc">
<b>What you are watching.</b> NASA battery <b>{demo_b}</b>, aged in a lab, replayed
cycle by cycle through our live pipeline. It was <i>kept out of training</i>, so the
AI has never seen it. Its per-cycle voltage, temperature and measured health are
NASA's real recordings; only the playback is sped up.<br><br>
<b>What the AI does.</b> Measuring battery health normally needs a full
charge-discharge test - hours per battery. Our {model_name} model estimates it from
data any BMS already logs, and explains each estimate.<br><br>
<b>How well.</b> {numbers}<br><br>
<b>Limits we know.</b> Trained on NASA lab cells; other cells need their own capacity
tests, which is our next step. Cycle count assumes a known history.
</div>""")

dash = {
    "title": "2 · AI Health Demo",
    "uid": "ev-battery-ageing-demo",
    "tags": ["battery", "demo", "ai"],
    "links": [{"title": "Modes", "type": "dashboards", "tags": ["battery"],
               "asDropdown": False, "includeVars": False, "keepTime": True,
               "targetBlank": False, "icon": "dashboard"}],
    "timezone": "browser",
    "editable": True,
    "refresh": "5s",
    "time": {"from": "now-30m", "to": "now"},
    "schemaVersion": 39,
    "version": 2,
    "panels": panels,
}

dash_style.add_header(panels, dash["uid"])
dash_style.finish(dash)

path = os.path.join(out_dir(), "ev-battery-ageing-demo.json")
with open(path, "w") as f:
    json.dump(dash, f, indent=2)
print(f"{len(panels)} panels written -> {os.path.relpath(path, HERE)}")
