"""
start_all.py - starts the whole digital twin stack.

    python start_all.py            REAL HARDWARE - data from the ESP32s
    python start_all.py sim        SIMULATION    - fake_publisher.py
    python start_all.py fault      SIMULATION with a loose connection after
                                   15 min - rehearse the resistance-jump alarm
    python start_all.py demo       AI DEMO       - replays NASA battery B0005,
                                   which the AI never saw in training, so
                                   judges watch AI estimate vs lab truth

The first run trains the AI (health_model.pkl) automatically - about
10-20 seconds.

Each program opens in its own titled window. Close a window to stop that
one program; close them all to stop everything.

Grafana dashboards are PROVISIONED FROM FILES now (see
grafana/provisioning/dashboards/), so they load themselves every time
Grafana starts. You never have to import a dashboard by hand again, and
they can't "go back to the old version" when a container is recreated.
"""

import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)

mode = (sys.argv[1].lower() if len(sys.argv) > 1 else "real")
if mode not in ("real", "sim", "fault", "demo"):
    print(f"Unknown mode '{mode}'. Use: real | sim | fault | demo")
    sys.exit(1)

PY = sys.executable          # the same python.exe that is running this

# (window title, command, only-in-these-modes, required file)
# The control panel and simulator are embedded in Grafana, so they use the
# same light "lab bench" look.
LIGHT = ["--theme.base", "light", "--theme.backgroundColor", "#ffffff",
         "--theme.secondaryBackgroundColor", "#f4f5f5", "--theme.textColor", "#24292e",
         "--theme.primaryColor", "#16181d"]

PROGRAMS = [
    ("1 Bridge (MQTT to InfluxDB)",
     [PY, "-u", "mqtt_to_influx.py"], None, "mqtt_to_influx.py"),

    ("2 Fake publisher",
     [PY, "-u", "fake_publisher.py"], {"sim"}, "fake_publisher.py"),

    ("2 Fake publisher (fault at 15 min)",
     [PY, "-u", "fake_publisher.py", "--fault"], {"fault"}, "fake_publisher.py"),

    ("2 NASA replay (AI demo)",
     [PY, "-u", "demo_nasa_replay.py"], {"demo"}, "demo_nasa_replay.py"),

    ("3 Twin engine",
     [PY, "-u", "twin_engine.py"], None, "twin_engine.py"),

    ("4 AI health estimator",
     [PY, "-u", "health_inference.py"], None, "health_inference.py"),

    ("5 Simulator :8502",
     [PY, "-m", "streamlit", "run", "simulator.py",
      "--server.port", "8502", "--server.headless", "true"] + LIGHT, None, "simulator.py"),

    ("6 Control :8503",
     [PY, "-m", "streamlit", "run", "control.py",
      "--server.port", "8503", "--server.headless", "true"] + LIGHT, None, "control.py"),

    ("7 3D model :8504",
     [PY, "-m", "streamlit", "run", "app.py",
      "--server.port", "8504", "--server.headless", "true"], None, "app.py"),

    ("8 Customer app :8505",
     [PY, "-m", "streamlit", "run", "app_user.py",
      "--server.port", "8505", "--server.headless", "true"], None, "app_user.py"),
]

# Files that must exist for the core stack to work at all
CORE_REQUIRED = ["docker-compose.yml", "mosquitto.conf", "mqtt_to_influx.py",
                 "twin_engine.py", "battery_model.py"]

# Dashboard files that should be provisioned - warn if missing
PROVISION_EXPECTED = [
    "grafana/provisioning/datasources/influxdb.yml",
    "grafana/provisioning/dashboards/dashboards.yml",
    "grafana/provisioning/dashboards/ev-battery-digital-twin.json",
    "grafana/provisioning/dashboards/ev-battery-ageing-demo.json",
]


def main():
    label = {"real": "REAL HARDWARE", "sim": "SIMULATION",
             "fault": "SIMULATION + LOOSE CONNECTION",
             "demo": "AI DEMO (NASA replay)"}[mode]
    print("=" * 60)
    print(f"  EV Battery Digital Twin  -  {label} mode")
    print("=" * 60)

    missing = [f for f in CORE_REQUIRED if not os.path.exists(f)]
    if missing:
        print("\nMissing core files in this folder:")
        for f in missing:
            print("   -", f)
        input("\nPress Enter to exit.")
        return

    missing_prov = [f for f in PROVISION_EXPECTED if not os.path.exists(f)]
    if missing_prov:
        print("\nGrafana dashboards will NOT load automatically - missing:")
        for f in missing_prov:
            print("   -", f)
        print("  (everything else will still run)\n")

    if not os.path.exists("health_model.pkl"):
        print("\nNo trained AI model yet - training it now (10-20 s)...")
        if subprocess.run([PY, "train_health_model.py"]).returncode != 0:
            print("\nTraining failed. Install the libraries and try again:")
            print("   python -m pip install xgboost scikit-learn pandas numpy matplotlib")
            input("Press Enter to exit.")
            return
    else:
        # Rebuild the dashboards from THIS laptop's trained model, so the
        # numbers and model name on them are always your own.
        for builder in ("build_dashboard.py", "build_demo_dashboard.py",
                        "build_whatif_dashboard.py"):
            if os.path.exists(builder):
                subprocess.run([PY, builder], stdout=subprocess.DEVNULL)

    print("\nStarting Docker containers...")
    if subprocess.run(["docker", "compose", "up", "-d"]).returncode != 0:
        print("\nDocker failed. Is Docker Desktop running?")
        input("Press Enter to exit.")
        return
    time.sleep(6)

    started, skipped = [], []
    # Data sources (fake publisher / replay) start LAST, so the bridge,
    # twin and AI are already listening when the first reading arrives.
    ordered = ([p for p in PROGRAMS if p[2] is None] +
               [p for p in PROGRAMS if p[2] is not None])
    for title, cmd, modes, required in ordered:
        if modes is not None and mode in modes:
            time.sleep(4)
        if modes is not None and mode not in modes:
            continue
        if not os.path.exists(required):
            skipped.append((title, required))
            continue
        subprocess.Popen(["cmd", "/k", "title", title, "&&"] + cmd,
                         creationflags=subprocess.CREATE_NEW_CONSOLE)
        started.append(title)
        print(f"  started : {title}")
        time.sleep(1)

    for title, required in skipped:
        print(f"  skipped : {title}  ({required} not in this folder)")

    print("\n" + "-" * 60)
    print("  OPEN THIS (app view):  http://localhost:3000/d/ev-battery-twin?kiosk&theme=light")
    print("      tabs at the top: Live pack | AI health demo | What-if simulator")
    print("      Esc = show Grafana's menus again")
    print("  3D model alone       http://localhost:8504")
    print("  Customer app         http://localhost:8505")
    print("  What-if simulator    http://localhost:8502")
    print("-" * 60)

    if mode == "fault":
        print("\n  FAULT REHEARSAL: the ESP32 may stay powered (it goes quiet in demo mode).")
        print("  At 15 min a 0.1 ohm loose connection appears; within about a")
        print("  minute the twin status turns RESISTANCE JUMP (red).")
    elif mode == "sim":
        print("\n  SIMULATION mode: the ESP32 may stay powered (it goes quiet in demo mode).")
    elif mode == "demo":
        print("\n  AI DEMO: the ESP32 may stay powered - the replay puts it in demo")
        print("  mode, so it stops sending its own readings and runs the AI on-chip.")
        print("  NASA battery B0005 (never seen by the AI) replays cycle by cycle.")
        print("  Open the 'AI Health Demo' dashboard: AI estimate vs lab truth,")
        print("  the reasons, cycles left, and the verdict (Warning near cycle 80).")
    else:
        print("\n  REAL HARDWARE mode: ESP32 powered and connected to WiFi/MQTT.")

    input("\nPress Enter to close this launcher (the programs keep running).")


if __name__ == "__main__":
    main()
