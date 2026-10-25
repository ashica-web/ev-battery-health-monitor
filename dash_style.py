"""
dash_style.py - the shared "lab bench" look of all three Grafana modes.

  add_header(panels, active_uid)   puts the header bar on top of a dashboard
  finish(dash)                     light-theme colours + shared settings

The look: light Grafana theme, black type, and colour only when something
needs attention (orange = warning, red = fault, green = the status words
"All clear" / "Tracking" / "Normal"). Headings use Big Shoulders, embedded
from fonts/ so it works without internet at the venue.

Tabs open each mode in Grafana's kiosk view (no Grafana menus).
Press Esc to get the menus back.
"""

import base64
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))

MODES = [
    ("ev-battery-twin",        "Live pack"),
    ("ev-battery-ageing-demo", "AI health demo"),
    ("ev-battery-whatif",      "What-if simulator"),
]
HEADER_H = 3

INK = "#16181d"
MUTED = "#5b616b"

# ---- colours: dark-theme values -> light-theme values ----
NAMED = {"orange": "#d9480f", "red": "#c92a2a", "yellow": "#b08800",
         "green": "#2b8a3e", "blue": "#1c64c4", "purple": "#7048e8"}
HEX = {"#8ab8ff": "#1c64c4", "#73bf69": "#2b8a3e", "#ff9830": "#d9480f",
       "#5794f2": "#1c7ed6", "#f2495c": "#c92a2a", "#b877d9": "#7048e8",
       "#8e8e8e": "#6c757d", "#ccccdc": "#24292e",
       "#181b1f": "#ffffff"}          # embedded-app backgrounds
                                      # (#0b0e14, the 3D viewport, stays dark)


def mode_url(uid):
    return f"/d/{uid}?kiosk&theme=light&refresh=5s"


def _fonts():
    faces = [("RigHead", "BigShoulders-Bold.sub.ttf", 700),
             ("RigHead", "BigShoulders-Regular.sub.ttf", 400),
             ("RigBody", "WorkSans-Regular.sub.ttf", 400)]
    css = ""
    for fam, fn, w in faces:
        p = os.path.join(HERE, "fonts", fn)
        if os.path.exists(p):
            b64 = base64.b64encode(open(p, "rb").read()).decode()
            css += (f"@font-face{{font-family:'{fam}';font-weight:{w};"
                    f"src:url(data:font/ttf;base64,{b64}) format('truetype')}}")
    return f"<style>{css}</style>" if css else ""


def header(active_uid):
    head = "'RigHead','Arial Narrow',Arial,sans-serif"
    body = "'RigBody',Arial,sans-serif"
    tabs = []
    for i, (uid, name) in enumerate(MODES):
        on = uid == active_uid
        tabs.append(
            f'<a href="{mode_url(uid)}" style="display:block;padding:7px 18px;'
            f'font-family:{head};font-weight:700;font-size:21px;line-height:1.2;'
            f'text-decoration:none;color:{"#fff" if on else INK};'
            f'background:{INK if on else "transparent"};'
            f'{"border-left:1.5px solid " + INK + ";" if i else ""}">{name}</a>')
    return (
        _fonts() +
        '<div style="height:100%;display:flex;align-items:center;'
        'justify-content:space-between;flex-wrap:wrap;gap:10px 24px;padding:0 4px">'
        '<div>'
        f'<div style="font-family:{head};font-weight:700;font-size:38px;line-height:1;'
        f'color:{INK}">EV battery digital twin</div>'
        f'<div style="font-family:{body};font-size:14px;color:{MUTED};margin-top:4px">'
        'Three Li-ion cells in series, watched by one ESP32, mirrored by a physics '
        'model and checked by an AI</div></div>'
        f'<div style="display:flex;border:1.5px solid {INK};border-radius:3px;'
        f'overflow:hidden">' + "".join(tabs) + '</div></div>')


def _shift(panel, dy):
    panel["gridPos"]["y"] += dy
    for sub in panel.get("panels", []):
        sub["gridPos"]["y"] += dy


def add_header(panels, active_uid):
    """Move every panel down and put the header bar on top (id 900)."""
    for p in panels:
        _shift(p, HEADER_H)
    panels.insert(0, {
        "type": "text", "title": "", "id": 900, "transparent": True,
        "gridPos": {"h": HEADER_H, "w": 24, "x": 0, "y": 0},
        "options": {"mode": "html", "content": header(active_uid)},
    })
    return panels


def _recolour(panel):
    """Light theme: neutral numbers in black, colour only for warnings."""
    fc = panel.get("fieldConfig", {})
    defaults = fc.get("defaults", {})
    is_stat = panel.get("type") == "stat"

    for st in defaults.get("thresholds", {}).get("steps", []):
        c = st.get("color")
        if is_stat and c in ("green", "blue"):
            st["color"] = "text"          # normal reading -> plain black
        elif c in NAMED:
            st["color"] = NAMED[c]

    def fix_mapping(maps):
        for m in maps or []:
            res = [m.get("options", {}).get("result")] if m.get("type") in ("range", "special") \
                else list(m.get("options", {}).values())
            for r in res:
                if isinstance(r, dict) and r.get("color") in NAMED:
                    r["color"] = NAMED[r["color"]]

    fix_mapping(defaults.get("mappings"))
    for ov in fc.get("overrides", []):
        for pr in ov.get("properties", []):
            if pr.get("id") == "mappings":
                fix_mapping(pr["value"])

    if is_stat:
        panel.setdefault("options", {})["colorMode"] = "value"


def finish(dash):
    """Settings and colours shared by all three modes."""
    dash["graphTooltip"] = 1          # hover one chart -> same moment on all
    dash["timezone"] = "browser"      # times in the laptop's clock (IST)
    dash.setdefault("refresh", "5s")
    for p in dash["panels"]:
        _recolour(p)
        for sub in p.get("panels", []):
            _recolour(sub)
    txt = json.dumps(dash["panels"])
    for old, new in HEX.items():
        txt = txt.replace(old, new).replace(old.upper(), new)
    dash["panels"] = json.loads(txt)
    return dash
