"""
Builds "3 · What-if Simulator" - the physics simulator as its own mode,
full screen, instead of a panel at the bottom of the live dashboard.
The mode buttons at the top of every dashboard switch between them.
"""
import json
import os

import dash_style

HERE = os.path.dirname(os.path.abspath(__file__))

panels = [
    {"type": "text", "title": "What-if simulator — physics model of the pack", "id": 1,
     "gridPos": {"h": 27, "w": 24, "x": 0, "y": 0},
     "options": {"mode": "html", "content":
                 '<iframe src="http://localhost:8502/?embed=true" '
                 'style="width:100%;height:100%;border:0;border-radius:4px;'
                 'background:#181b1f"></iframe>'}},
    {"type": "text", "title": "What this mode is for", "id": 2,
     "gridPos": {"h": 4, "w": 24, "x": 0, "y": 27},
     "options": {"mode": "html", "content": """
<div style="font-size:14px;line-height:1.7;padding:4px 10px;color:#ccccdc">
Ask questions you would never risk on real cells: a 3C discharge at 45 °C,
a pack at 70% health, a cooling failure. The same physics model that runs
the live twin predicts runtime, peak temperature and voltage sag - nothing
here touches the real battery.</div>"""}},
]

dash = {
    "title": "3 · What-if Simulator",
    "uid": "ev-battery-whatif",
    "tags": ["battery", "simulator"],
    "links": [{"title": "Modes", "type": "dashboards", "tags": ["battery"],
               "asDropdown": False, "includeVars": False, "keepTime": True,
               "targetBlank": False, "icon": "dashboard"}],
    "editable": True,
    "time": {"from": "now-30m", "to": "now"},
    "schemaVersion": 39,
    "version": 1,
    "panels": panels,
}

dash_style.add_header(panels, dash["uid"])
dash_style.finish(dash)

prov = os.path.join(HERE, "grafana", "provisioning", "dashboards")
path = os.path.join(prov if os.path.isdir(prov) else HERE, "ev-battery-whatif.json")
with open(path, "w") as f:
    json.dump(dash, f, indent=2)
print(f"{len(panels)} panels written -> {os.path.relpath(path, HERE)}")
