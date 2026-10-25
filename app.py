"""
app.py - live 3D digital twin of the battery pack (Three.js in Streamlit)  v2

Model design by the team. Live-data plumbing reworked so it actually
updates:

  * The 3D scene is built ONCE. The browser connects straight to MQTT
    over WebSockets (port 9001) and updates colours in place - no
    flicker, and the camera stays where you leave it.
  * Swelling uses the CHANGE in piezo reading from its starting value,
    not the raw number (raw values around 2350 made every cell look
    swollen).
  * Gas smoke appears on the firmware's gas alarm or a high reading,
    not at a normal background level.

Needs Mosquitto's WebSocket listener on port 9001 (see mosquitto.conf).

Run:  python -m streamlit run app.py --server.port 8504 --server.headless true
"""

import streamlit as st
import streamlit.components.v1 as components

# ================= CONFIG =================
BROKER_HOST = "localhost"   # address the BROWSER uses to reach Mosquitto
BROKER_WS_PORT = 9001
VIEW_HEIGHT = 560           # pixels - match your Grafana panel height
GAS_WARN_LEVEL = 1500       # show smoke above this (firmware alarm is 2500)
# ==========================================

st.set_page_config(page_title="3D twin", layout="wide",
                   initial_sidebar_state="collapsed")

st.markdown("""
<style>
    #MainMenu, footer, header {visibility: hidden;}
    [data-testid="stHeader"], [data-testid="stSidebar"],
    [data-testid="stToolbar"] {display: none;}
    div.block-container {padding: 0 !important; margin: 0 !important;}
    body, html, .stApp {margin: 0; padding: 0; overflow: hidden;
                        background-color: #181b1f;}
</style>
""", unsafe_allow_html=True)

HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #181b1f; font-family: system-ui, sans-serif; overflow: hidden; }
  #c { width: 100vw; height: 100vh; position: absolute; top: 0; left: 0; }
  #hud { position: absolute; top: 10px; left: 12px; font-size: 12px;
         color: #9aa5b1; line-height: 1.6; pointer-events: none;
         background: rgba(0,0,0,.35); padding: 6px 10px; border-radius: 6px; }
  #hud b { color: #e5e7eb; font-variant-numeric: tabular-nums; }
  .dot { display:inline-block; width:8px; height:8px; border-radius:50%;
         margin-right:6px; background:#6b7280; }
  .live { background:#22c55e; } .down { background:#ef4444; }
</style>
</head>
<body>
<div id="c"></div>
<div id="hud">
  <span id="dot" class="dot"></span><span id="conn">connecting</span><br>
  Cells <b id="temps">-- / -- / -- &deg;C</b><br>
  Pack <b id="pack">-- V</b> &middot; SOC <b id="soc">--</b> &middot; I <b id="cur">-- A</b><br>
  Gas <b id="gas">--</b> &middot; Twin <b id="twin">--</b> &middot; AI <b id="ai">--</b>
</div>

<script src="https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/three@0.128.0/examples/js/controls/OrbitControls.js"></script>
<script src="https://unpkg.com/mqtt@5/dist/mqtt.min.js"></script>

<script>
const BROKER = "ws://%%HOST%%:%%PORT%%";
const GAS_WARN = %%GAS%%;

// ---------------- scene (built once) ----------------
const container = document.getElementById('c');
const scene = new THREE.Scene();
scene.background = new THREE.Color(0x181b1f);
scene.fog = new THREE.FogExp2(0x181b1f, 0.04);

const camera = new THREE.PerspectiveCamera(45, window.innerWidth / window.innerHeight, 0.1, 100);
camera.position.set(0, 2.5, 4.5);

const renderer = new THREE.WebGLRenderer({ antialias: true });
renderer.setSize(window.innerWidth, window.innerHeight);
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
renderer.shadowMap.enabled = true;
renderer.shadowMap.type = THREE.PCFSoftShadowMap;
renderer.toneMapping = THREE.ACESFilmicToneMapping;
renderer.toneMappingExposure = 1.15;
container.appendChild(renderer.domElement);

const controls = new THREE.OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;
controls.dampingFactor = 0.05;
controls.target.set(0, 0.85, 0);

// Lighting
const keyLight = new THREE.DirectionalLight(0xffffff, 1.5);
keyLight.position.set(5, 8, 5);
keyLight.castShadow = true;
keyLight.shadow.mapSize.width = 2048;
keyLight.shadow.mapSize.height = 2048;
scene.add(keyLight);
const fillLight = new THREE.DirectionalLight(0xfff5ea, 0.9);
fillLight.position.set(-6, 4, -3);
scene.add(fillLight);
const rimLight = new THREE.DirectionalLight(0xc084fc, 1.4);
rimLight.position.set(0, 5, -6);
scene.add(rimLight);
scene.add(new THREE.AmbientLight(0xffffff, 0.7));

// Floor
const grid = new THREE.GridHelper(16, 32, 0x2c3238, 0x22252b);
scene.add(grid);
const shadowPlane = new THREE.Mesh(new THREE.PlaneGeometry(20, 20),
                                   new THREE.ShadowMaterial({ opacity: 0.25 }));
shadowPlane.rotation.x = -Math.PI / 2;
shadowPlane.receiveShadow = true;
scene.add(shadowPlane);

const mainGroup = new THREE.Group();
scene.add(mainGroup);

// Materials
const frameMat    = new THREE.MeshStandardMaterial({ color: 0x111827, metalness: 0.6, roughness: 0.3 });
const terminalMat = new THREE.MeshStandardMaterial({ color: 0xf8fafc, metalness: 0.95, roughness: 0.05 });
const copperMat   = new THREE.MeshStandardMaterial({ color: 0xd97706, metalness: 0.9, roughness: 0.2 });
const pcbMat      = new THREE.MeshStandardMaterial({ color: 0x047857, metalness: 0.2, roughness: 0.5 });
const sensorMat   = new THREE.MeshStandardMaterial({ color: 0x1f2937, metalness: 0.7, roughness: 0.2 });
const piezoMat    = new THREE.MeshStandardMaterial({ color: 0xf59e0b, metalness: 0.5, roughness: 0.3 });

// Temperature -> colour, smooth per degree.
// Cool end keeps the team's purple; hot end goes to red.
const STOPS = [[20, 0x7C3AED], [30, 0x16a34a], [40, 0xeab308], [48, 0xd97706], [55, 0xdc2626]];
function tempColor(t) {
  if (t <= STOPS[0][0]) return new THREE.Color(STOPS[0][1]);
  for (let i = 0; i < STOPS.length - 1; i++) {
    const [t0, c0] = STOPS[i], [t1, c1] = STOPS[i + 1];
    if (t <= t1) {
      const c = new THREE.Color(c0);
      return c.lerp(new THREE.Color(c1), (t - t0) / (t1 - t0));
    }
  }
  return new THREE.Color(STOPS[STOPS.length - 1][1]);
}

// Frame
const baseGeo = new THREE.BoxGeometry(1.85, 0.12, 0.78);
const baseMesh = new THREE.Mesh(baseGeo, frameMat);
baseMesh.position.set(0, 0.06, 0);
baseMesh.castShadow = true; baseMesh.receiveShadow = true;
mainGroup.add(baseMesh);
const topFrame = new THREE.Mesh(baseGeo, frameMat);
topFrame.position.set(0, 1.64, 0);
topFrame.castShadow = true;
mainGroup.add(topFrame);
const postGeo = new THREE.BoxGeometry(0.08, 1.46, 0.08);
[[-0.88, 0.85, 0.34], [0.88, 0.85, 0.34], [-0.88, 0.85, -0.34], [0.88, 0.85, -0.34]]
  .forEach(p => { const m = new THREE.Mesh(postGeo, frameMat); m.position.set(...p);
                  m.castShadow = true; mainGroup.add(m); });

// Cells - kept in an array so their colour and size can be updated live
const cellGeo = new THREE.CylinderGeometry(0.21, 0.21, 1.45, 64);
const cells = [];
[-0.52, 0, 0.52].forEach(x => {
  const mat = new THREE.MeshStandardMaterial({ color: 0x7C3AED, metalness: 0.25, roughness: 0.18 });
  const cell = new THREE.Mesh(cellGeo, mat);
  cell.position.set(x, 0.85, 0);
  cell.castShadow = true;
  mainGroup.add(cell);
  cells.push(cell);

  const cap = new THREE.Mesh(new THREE.CylinderGeometry(0.085, 0.085, 0.06, 32), terminalMat);
  cap.position.set(x, 1.6, 0);
  mainGroup.add(cap);

  const ntc = new THREE.Mesh(new THREE.BoxGeometry(0.04, 0.1, 0.03), sensorMat);
  ntc.position.set(x, 0.85, 0.22);
  mainGroup.add(ntc);
});

// Charge gauges - one per cell, height follows that cell's voltage
const GAUGE_H = 1.3;
const gaugeShellGeo = new THREE.BoxGeometry(0.07, GAUGE_H, 0.04);
const gaugeFillGeo = new THREE.BoxGeometry(0.05, GAUGE_H, 0.05);
gaugeFillGeo.translate(0, GAUGE_H / 2, 0);            // grow from the bottom
const gauges = [];
[-0.52, 0, 0.52].forEach(x => {
  const shell = new THREE.Mesh(gaugeShellGeo,
    new THREE.MeshStandardMaterial({ color: 0x1f2937, transparent: true, opacity: 0.55 }));
  shell.position.set(x + 0.28, 0.85, 0.3);
  mainGroup.add(shell);
  const fill = new THREE.Mesh(gaugeFillGeo,
    new THREE.MeshStandardMaterial({ color: 0x22c55e, emissive: 0x22c55e, emissiveIntensity: 0.35 }));
  fill.position.set(x + 0.28, 0.85 - GAUGE_H / 2, 0.3);
  fill.scale.y = 0.001;
  mainGroup.add(fill);
  gauges.push(fill);
});

// Current-flow particles along the busbar
const FLOW_N = 24;
const flowGeo = new THREE.BufferGeometry();
const flowPos = new Float32Array(FLOW_N * 3);
for (let i = 0; i < FLOW_N; i++) {
  flowPos[i * 3]     = -0.57 + (i / FLOW_N) * 1.14;
  flowPos[i * 3 + 1] = 1.665;
  flowPos[i * 3 + 2] = (Math.random() - 0.5) * 0.08;
}
flowGeo.setAttribute('position', new THREE.BufferAttribute(flowPos, 3));
const flow = new THREE.Points(flowGeo,
  new THREE.PointsMaterial({ color: 0x38bdf8, size: 0.05, transparent: true, opacity: 0.9 }));
flow.visible = false;
mainGroup.add(flow);

// Piezo sensors
const piezoGeo = new THREE.BoxGeometry(0.02, 0.6, 0.28);
[-0.26, 0.26].forEach(x => { const m = new THREE.Mesh(piezoGeo, piezoMat);
                             m.position.set(x, 0.85, 0); mainGroup.add(m); });

// Busbar
const busbar = new THREE.Mesh(new THREE.BoxGeometry(1.15, 0.018, 0.12), copperMat);
busbar.position.set(0, 1.635, 0);
busbar.castShadow = true;
mainGroup.add(busbar);

// PCB
const pcb = new THREE.Mesh(new THREE.BoxGeometry(1.3, 0.03, 0.5), pcbMat);
pcb.position.set(-0.15, 1.73, 0);
pcb.castShadow = true;
mainGroup.add(pcb);

// MQ2 gas sensor
const mq2Base = new THREE.Mesh(new THREE.CylinderGeometry(0.09, 0.09, 0.08, 32), sensorMat);
mq2Base.position.set(-0.5, 1.785, 0.1);
mainGroup.add(mq2Base);
const mq2Cap = new THREE.Mesh(new THREE.CylinderGeometry(0.075, 0.075, 0.08, 32),
  new THREE.MeshStandardMaterial({ color: 0xf1f5f9, metalness: 0.9, roughness: 0.1 }));
mq2Cap.position.set(-0.5, 1.865, 0.1);
mainGroup.add(mq2Cap);

// Gas particles
const pCount = 50;
const pGeo = new THREE.BufferGeometry();
const pPos = new Float32Array(pCount * 3);
for (let i = 0; i < pCount * 3; i += 3) {
  pPos[i]     = -0.5 + (Math.random() - 0.5) * 0.25;
  pPos[i + 1] = 1.9 + Math.random() * 0.4;
  pPos[i + 2] = 0.1 + (Math.random() - 0.5) * 0.25;
}
pGeo.setAttribute('position', new THREE.BufferAttribute(pPos, 3));
const gasParticles = new THREE.Points(pGeo,
  new THREE.PointsMaterial({ color: 0x94a3b8, size: 0.06, transparent: true, opacity: 0.65 }));
gasParticles.visible = false;
mainGroup.add(gasParticles);

// ---------------- live updates ----------------
let piezoBase = null;
const targetScale = [1, 1, 1];
const targetFill  = [0, 0, 0];
const cellFault   = [false, false, false];
let packAlarm = false;
let flowSpeed = 0;

function applyTelemetry(d) {
  const temps = [d.cell1_temp, d.cell2_temp, d.cell3_temp].map(t => (t && t > -50) ? t : 25);
  const volts = [d.cell1_voltage, d.cell2_voltage, d.cell3_voltage].map(v => v || 0);

  temps.forEach((t, i) => {
    cells[i].material.color.copy(tempColor(t));

    // charge gauge: this cell's voltage, 3.0 V empty -> 4.2 V full
    let level;
    if (volts[i] > 0.5) level = (volts[i] - 3.0) / 1.2;
    else level = (d.soc_percent || 0) / 100;       // voltage not wired: use pack SOC
    targetFill[i] = Math.max(0.02, Math.min(1, level));

    // per-cell fault: overtemp, or voltage out of range
    const vBad = volts[i] > 0.5 && (volts[i] < 2.8 || volts[i] > 4.25);
    cellFault[i] = t > 55 || vBad;
  });

  packAlarm = d.safety_active === 1 || d.bms_tripped === 1 || d.fault_gas === 1;

  // Swelling: change from the first reading, not the raw value
  const p = Math.max(d.piezo1_raw || 0, d.piezo2_raw || 0);
  if (p > 0) {
    if (piezoBase === null) piezoBase = p;
    targetScale.fill(1 + Math.min(Math.max(0, p - piezoBase) / 1000, 0.22));
  }

  // Current flow: speed follows current, direction follows sign
  const I = d.current_A || 0;
  flowSpeed = Math.abs(I) < 0.05 ? 0 : Math.sign(I) * Math.min(Math.abs(I), 3) * 0.004;
  flow.visible = flowSpeed !== 0;

  const gas = d.gas_level || 0;
  gasParticles.visible = (d.fault_gas === 1) || (gas > GAS_WARN);

  document.getElementById('temps').innerHTML = temps.map(t => t.toFixed(1)).join(' / ') + ' &deg;C';
  document.getElementById('pack').textContent = (d.pack_voltage || 0).toFixed(2) + ' V';
  document.getElementById('soc').textContent  = (d.soc_percent || 0).toFixed(1) + ' %';
  document.getElementById('cur').textContent  = I.toFixed(2) + ' A';
  document.getElementById('gas').textContent  = gas;
}

function applyTwin(d) {
  const map = { tracking: 'tracking', drifting: 'drifting', divergent: 'DIVERGENT',
                no_data: 'no voltage', implausible: 'check wiring', replay: 'lab replay',
                resistance_jump: 'RESISTANCE JUMP' };
  document.getElementById('twin').textContent = map[d.status] || d.status || '--';
}

function applyAI(d) {
  if (!d.prediction) return;
  const el = document.getElementById('ai');
  const est = (typeof d.est_soh === 'number') ? ' \u00b7 health ' + d.est_soh.toFixed(0) + '%' : '';
  el.textContent = d.prediction + est;
  el.style.color = d.prediction === 'Critical' ? '#f87171'
                 : d.prediction === 'Warning'  ? '#fbbf24' : '#4ade80';
}

function setConn(text, cls) {
  document.getElementById('conn').textContent = text;
  document.getElementById('dot').className = 'dot ' + cls;
}

try {
  const client = mqtt.connect(BROKER, {
    clientId: 'twin3d_' + Math.random().toString(16).slice(2, 10),
    reconnectPeriod: 3000
  });
  client.on('connect', () => { setConn('live', 'live');
    client.subscribe(['battery/state', 'battery/twin', 'battery/ai']); });
  client.on('reconnect', () => setConn('reconnecting', ''));
  client.on('offline', () => setConn('offline - is port 9001 open?', 'down'));
  client.on('error', () => setConn('MQTT error', 'down'));
  client.on('message', (topic, payload) => {
    try {
      const d = JSON.parse(payload.toString());
      if (topic === 'battery/state') applyTelemetry(d);
      else if (topic === 'battery/twin') applyTwin(d);
      else if (topic === 'battery/ai') applyAI(d);
    } catch (e) {}
  });
} catch (e) {
  setConn('MQTT library failed to load', 'down');
}

// ---------------- render loop ----------------
function animate() {
  requestAnimationFrame(animate);
  mainGroup.rotation.y += 0.004;

  const now = performance.now() / 1000;
  const pulse = 0.5 + 0.5 * Math.sin(now * 6);

  cells.forEach((c, i) => {
    const s = c.scale.x + (targetScale[i] - c.scale.x) * 0.05;   // swelling
    c.scale.set(s, 1, s);

    const m = c.material;                                         // fault glow
    if (cellFault[i])      { m.emissive.setHex(0xdc2626); m.emissiveIntensity = 0.2 + 0.7 * pulse; }
    else if (packAlarm)    { m.emissive.setHex(0xf59e0b); m.emissiveIntensity = 0.1 + 0.4 * pulse; }
    else                   { m.emissive.setHex(0x000000); m.emissiveIntensity = 0; }

    const g = gauges[i];                                          // charge gauge
    g.scale.y += (targetFill[i] - g.scale.y) * 0.08;
    const lvl = g.scale.y;
    const gc = lvl > 0.5 ? 0x22c55e : (lvl > 0.2 ? 0xeab308 : 0xef4444);
    g.material.color.setHex(gc);
    g.material.emissive.setHex(gc);
  });

  if (flow.visible) {                                             // current flow
    const fp = flow.geometry.attributes.position.array;
    for (let i = 0; i < FLOW_N; i++) {
      fp[i * 3] += flowSpeed;
      if (fp[i * 3] > 0.57)  fp[i * 3] = -0.57;
      if (fp[i * 3] < -0.57) fp[i * 3] = 0.57;
    }
    flow.geometry.attributes.position.needsUpdate = true;
  }

  if (gasParticles.visible) {
    const pos = gasParticles.geometry.attributes.position.array;
    for (let i = 1; i < pCount * 3; i += 3) {
      pos[i] += 0.012;
      if (pos[i] > 3.0) pos[i] = 1.9;
    }
    gasParticles.geometry.attributes.position.needsUpdate = true;
  }

  controls.update();
  renderer.render(scene, camera);
}

window.addEventListener('resize', () => {
  camera.aspect = window.innerWidth / window.innerHeight;
  camera.updateProjectionMatrix();
  renderer.setSize(window.innerWidth, window.innerHeight);
});

animate();
</script>
</body>
</html>
"""

html = (HTML.replace("%%HOST%%", BROKER_HOST)
            .replace("%%PORT%%", str(BROKER_WS_PORT))
            .replace("%%GAS%%", str(GAS_WARN_LEVEL)))

# Rendered ONCE. Live data flows over MQTT inside the page itself.
components.html(html, height=VIEW_HEIGHT, scrolling=False)
