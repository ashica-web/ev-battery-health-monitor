"""
fix_dashboards.py - makes Grafana show the NEW dashboards.

    python fix_dashboards.py

Run it from the battery twin folder while Docker Desktop is open.
It:
  1. moves any OLD dashboard files out of grafana/provisioning/dashboards
     (unzipping a new version adds files but never deletes old ones, so
     Grafana could still load an old dashboard with the same ID)
  2. rebuilds the three new dashboards from your trained model
  3. recreates the Grafana container from THIS folder
  4. removes old copies of the battery dashboards saved inside Grafana
     (each one is backed up first, nothing is lost)
  5. checks that the new dashboard is the one being served, and opens it

Old files and backups go to grafana/old_dashboards_backup/.
"""

import base64
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
PY = sys.executable
PROV = os.path.join("grafana", "provisioning", "dashboards")
BACKUP = os.path.join("grafana", "old_dashboards_backup")
KEEP = {"ev-battery-digital-twin.json", "ev-battery-ageing-demo.json",
        "ev-battery-whatif.json"}
OUR_UIDS = {"ev-battery-twin", "ev-battery-ageing-demo", "ev-battery-whatif"}
GRAFANA = "http://localhost:3000"
USER, PASSWORD = "admin", "battery123"
APP_LINK = GRAFANA + "/d/ev-battery-twin?kiosk&theme=light"


def say(step, text):
    print(f"\n[{step}] {text}")


def api(method, path):
    req = urllib.request.Request(GRAFANA + path, method=method)
    token = base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
    req.add_header("Authorization", "Basic " + token)
    with urllib.request.urlopen(req, timeout=10) as r:
        body = r.read().decode() or "{}"
        return json.loads(body)


def main():
    os.makedirs(BACKUP, exist_ok=True)

    # ---- 1. old files out of the provisioning folder ----
    say(1, "Looking for old dashboard files")
    moved = 0
    for f in sorted(os.listdir(PROV)):
        if f.endswith(".json") and f not in KEEP:
            shutil.move(os.path.join(PROV, f), os.path.join(BACKUP, f))
            print(f"   moved old file: {f}")
            moved += 1
    print(f"   {moved} old file(s) moved" if moved else "   none found")

    # ---- 2. rebuild the new dashboards ----
    say(2, "Building the new dashboards")
    for b in ("build_dashboard.py", "build_demo_dashboard.py", "build_whatif_dashboard.py"):
        r = subprocess.run([PY, b], capture_output=True, text=True)
        print("   " + (r.stdout.strip() or r.stderr.strip()[-300:]))
        if r.returncode != 0:
            print("   BUILD FAILED - send this message to Claude.")
            return
    d = json.load(open(os.path.join(PROV, "ev-battery-digital-twin.json")))
    print(f"   new dashboard: '{d['title']}', {len(d['panels'])} panels")

    # ---- 3. Grafana container from THIS folder ----
    say(3, "Restarting Grafana from this folder (your data is kept)")
    subprocess.run(["docker", "rm", "-f", "battery-grafana"],
                   capture_output=True, text=True)
    r = subprocess.run(["docker", "compose", "up", "-d", "grafana"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stderr[-800:])
        print("   Docker failed. Is Docker Desktop open?")
        return
    print("   waiting for Grafana", end="", flush=True)
    for _ in range(60):
        try:
            urllib.request.urlopen(GRAFANA + "/api/health", timeout=2)
            break
        except Exception:
            print(".", end="", flush=True)
            time.sleep(2)
    else:
        print("\n   Grafana did not start. Check Docker Desktop.")
        return
    print(" up")
    time.sleep(5)       # let it finish loading the dashboard files

    # ---- 4. old copies stored inside Grafana ----
    say(4, "Removing old battery dashboards saved inside Grafana")
    try:
        found = api("GET", "/api/search?type=dash-db&limit=500")
    except urllib.error.HTTPError as e:
        print(f"   could not log in to Grafana ({e.code}) - skipped this step.")
        found = []
    removed = 0
    for item in found:
        uid, title = item.get("uid"), item.get("title", "")
        try:
            full = api("GET", f"/api/dashboards/uid/{uid}")
        except urllib.error.HTTPError:
            continue
        if full.get("meta", {}).get("provisioned"):
            continue            # loaded from our files - the new ones
        tags = full.get("dashboard", {}).get("tags", [])
        if uid in OUR_UIDS or "battery" in tags or "battery" in title.lower():
            with open(os.path.join(BACKUP, f"saved_{uid}.json"), "w") as fh:
                json.dump(full, fh, indent=2)
            try:
                api("DELETE", f"/api/dashboards/uid/{uid}")
                print(f"   removed old copy: '{title}' (backup saved)")
                removed += 1
            except urllib.error.HTTPError as e:
                print(f"   could not remove '{title}' ({e.code})")
    print(f"   {removed} removed" if removed else "   none found")

    # ---- 5. check what Grafana is serving now ----
    say(5, "Checking")
    ok = True
    for uid in sorted(OUR_UIDS):
        try:
            full = api("GET", f"/api/dashboards/uid/{uid}")
            dash = full["dashboard"]
            has_header = any(p.get("id") == 900 for p in dash.get("panels", []))
            print(f"   {uid:24s} '{dash['title']}'  "
                  f"{'NEW' if has_header else 'OLD'}")
            ok &= has_header
        except urllib.error.HTTPError as e:
            print(f"   {uid:24s} not found yet ({e.code})")
            ok = False
    if ok:
        print("\nDone. Open:  " + APP_LINK)
        print("Then press Ctrl+F5 once, and F11 for full screen.")
        webbrowser.open(APP_LINK)
    else:
        print("\nNot fixed yet. Wait 30 seconds and run this again; if it still")
        print("says OLD, send a screenshot of this window to Claude.")


if __name__ == "__main__":
    main()
