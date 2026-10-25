"""
battery_model.py
================
Thevenin equivalent-circuit model of a lithium-ion cell, plus a simple
lumped thermal model.

This is the piece that turns a monitoring system into a digital twin.
It predicts what the battery SHOULD do, so you can compare against what
it actually does, and so you can ask questions you never physically ran.

The model
---------
    V_terminal = OCV(SOC) - I*R0 - V_rc

    dV_rc/dt = I/C1 - V_rc/(R1*C1)          (transient response)
    dSOC/dt  = -I / (3600 * Q)              (coulomb counting)
    dT/dt    = (I^2*R0 - h*A*(T - T_amb)) / (m*cp)   (self-heating)

R0 is the instantaneous resistance — it causes the immediate voltage
step when current changes. R1/C1 capture the slower relaxation over
seconds to minutes. R0 rising over cycles is the single best
degradation indicator in battery science.

No external dependencies beyond the standard library.
"""

import math
from dataclasses import dataclass, field


# ---------------------------------------------------------------
# Open-circuit voltage curve
# ---------------------------------------------------------------
# Typical NMC/LCO 18650. Replace with your own measured curve once you
# have one (rest the cell at each SOC, record the settled voltage).

_OCV_TABLE = [
    (0.00, 3.00), (0.05, 3.35), (0.10, 3.45), (0.15, 3.51),
    (0.20, 3.55), (0.30, 3.62), (0.40, 3.68), (0.50, 3.74),
    (0.60, 3.82), (0.70, 3.90), (0.80, 3.99), (0.90, 4.09),
    (0.95, 4.15), (1.00, 4.20),
]


def ocv(soc_fraction):
    """Open-circuit voltage of ONE cell at a given SOC (0.0 - 1.0)."""
    s = max(0.0, min(1.0, soc_fraction))

    for i in range(len(_OCV_TABLE) - 1):
        s_lo, v_lo = _OCV_TABLE[i]
        s_hi, v_hi = _OCV_TABLE[i + 1]
        if s_lo <= s <= s_hi:
            f = (s - s_lo) / (s_hi - s_lo)
            return v_lo + (v_hi - v_lo) * f
    return _OCV_TABLE[-1][1]


def soc_from_ocv(voltage):
    """Inverse lookup — used to correct coulomb-counting drift after a rest."""
    v = max(_OCV_TABLE[0][1], min(_OCV_TABLE[-1][1], voltage))

    for i in range(len(_OCV_TABLE) - 1):
        s_lo, v_lo = _OCV_TABLE[i]
        s_hi, v_hi = _OCV_TABLE[i + 1]
        if v_lo <= v <= v_hi:
            f = (v - v_lo) / (v_hi - v_lo)
            return s_lo + (s_hi - s_lo) * f
    return 1.0


# ---------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------

@dataclass
class CellParams:
    """Electrical and thermal parameters for one cell.

    Defaults are reasonable for a healthy 18650. Replace R0/R1/C1 with
    values from your own pulse test — see calibrate_r0() below.
    """
    capacity_Ah: float = 2.5      # nominal capacity
    R0_ohm: float      = 0.045    # instantaneous resistance
    R1_ohm: float      = 0.020    # polarisation resistance
    C1_farad: float    = 800.0    # polarisation capacitance

    # Thermal
    mass_kg: float      = 0.045   # an 18650 weighs about 45 g
    cp_J_per_kgK: float = 900.0   # specific heat, li-ion cell
    hA_W_per_K: float   = 0.09    # convective loss, still air

    # Ageing multipliers — how much worse than new
    soh_fraction: float = 1.0     # 1.0 = new, 0.8 = end of EV life

    @property
    def R0_aged(self):
        """R0 grows as the cell ages. Roughly doubles by 80% SOH."""
        if self.soh_fraction >= 1.0:
            return self.R0_ohm
        return self.R0_ohm * (1.0 + 5.0 * (1.0 - self.soh_fraction))

    @property
    def capacity_aged(self):
        return self.capacity_Ah * self.soh_fraction


@dataclass
class CellState:
    """Mutable state, advanced one step at a time."""
    soc: float = 1.0           # 0.0 - 1.0
    v_rc: float = 0.0          # voltage across the RC pair
    temp_C: float = 25.0
    terminal_V: float = 4.2


# ---------------------------------------------------------------
# The model
# ---------------------------------------------------------------

class ThreveninCell:
    """One cell. Call step() repeatedly with the current draw."""

    def __init__(self, params=None, state=None):
        self.p = params or CellParams()
        self.s = state or CellState()

    def step(self, current_A, ambient_C, dt_s):
        """Advance the model by dt_s seconds.

        current_A: positive = discharging, negative = charging
        Returns the predicted terminal voltage.
        """
        p, s = self.p, self.s

        # --- SOC via coulomb counting ---
        dsoc = -(current_A * dt_s) / (3600.0 * p.capacity_aged)
        s.soc = max(0.0, min(1.0, s.soc + dsoc))

        # --- RC branch (exponential relaxation) ---
        tau = p.R1_ohm * p.C1_farad
        v_rc_inf = current_A * p.R1_ohm
        alpha = math.exp(-dt_s / tau) if tau > 0 else 0.0
        s.v_rc = s.v_rc * alpha + v_rc_inf * (1.0 - alpha)

        # --- terminal voltage ---
        s.terminal_V = ocv(s.soc) - current_A * p.R0_aged - s.v_rc

        # --- thermal ---
        heat_W = (current_A ** 2) * p.R0_aged          # I^2 R self-heating
        cool_W = p.hA_W_per_K * (s.temp_C - ambient_C)
        dT = (heat_W - cool_W) * dt_s / (p.mass_kg * p.cp_J_per_kgK)
        s.temp_C += dT

        return s.terminal_V

    def reset(self, soc=1.0, temp_C=25.0):
        self.s = CellState(soc=soc, temp_C=temp_C,
                           terminal_V=ocv(soc))


class BatteryPack:
    """Three cells in series, which is what your prototype is."""

    def __init__(self, n_cells=3, params=None):
        self.cells = [ThreveninCell(params or CellParams())
                      for _ in range(n_cells)]

    def step(self, current_A, ambient_C, dt_s):
        """Series pack: same current through every cell."""
        voltages = [c.step(current_A, ambient_C, dt_s) for c in self.cells]
        return sum(voltages)

    def reset(self, soc=1.0, temp_C=25.0):
        for c in self.cells:
            c.reset(soc, temp_C)

    @property
    def pack_voltage(self):
        return sum(c.s.terminal_V for c in self.cells)

    @property
    def soc(self):
        return sum(c.s.soc for c in self.cells) / len(self.cells)

    @property
    def max_temp(self):
        return max(c.s.temp_C for c in self.cells)


# ---------------------------------------------------------------
# Simulation — the "what if" capability
# ---------------------------------------------------------------

def simulate_discharge(c_rate=1.0,
                       ambient_C=25.0,
                       soh=1.0,
                       start_soc=1.0,
                       cutoff_V_per_cell=2.8,
                       n_cells=3,
                       dt_s=1.0,
                       max_seconds=20000):
    """Run a constant-current discharge and return the full trace.

    This is what a physical rig CANNOT give you safely. You can ask for
    3C at 45 degrees on an 80%-SOH pack and get an answer in a second,
    without risking a cell.

    c_rate: 1.0 = discharge at the cell's rated capacity in one hour
    Returns a dict of lists, ready to plot.
    """
    params = CellParams(soh_fraction=soh)
    pack = BatteryPack(n_cells=n_cells, params=params)
    pack.reset(soc=start_soc, temp_C=ambient_C)

    current_A = c_rate * params.capacity_aged
    cutoff_pack_V = cutoff_V_per_cell * n_cells

    trace = {"t_s": [], "pack_V": [], "soc": [], "temp_C": [],
             "current_A": [], "power_W": []}

    t = 0.0
    while t < max_seconds:
        v = pack.step(current_A, ambient_C, dt_s)

        trace["t_s"].append(t)
        trace["pack_V"].append(v)
        trace["soc"].append(pack.soc * 100.0)
        trace["temp_C"].append(pack.max_temp)
        trace["current_A"].append(current_A)
        trace["power_W"].append(v * current_A)

        if v <= cutoff_pack_V or pack.soc <= 0.001:
            break
        t += dt_s

    trace["runtime_s"] = t
    trace["runtime_min"] = t / 60.0
    trace["delivered_Ah"] = current_A * t / 3600.0
    trace["peak_temp_C"] = max(trace["temp_C"]) if trace["temp_C"] else ambient_C

    return trace


def simulate_fault(fault_type, **kwargs):
    """Inject a fault into the MODEL and see what it would look like.

    Lets you show a judge what a failing cell looks like on the dashboard
    without damaging a real one.

    fault_type:
      'high_resistance'  — R0 doubled, as in an aged or damaged cell
      'capacity_fade'    — 70% SOH
      'thermal_runaway'  — cooling fails, heat accumulates
    """
    if fault_type == "high_resistance":
        p = CellParams()
        p.R0_ohm *= 2.5
        return simulate_discharge(**kwargs), p

    if fault_type == "capacity_fade":
        kwargs["soh"] = 0.70
        return simulate_discharge(**kwargs), CellParams(soh_fraction=0.70)

    if fault_type == "thermal_runaway":
        p = CellParams()
        p.hA_W_per_K = 0.005      # cooling essentially gone
        pack = BatteryPack(params=p)
        pack.reset(temp_C=kwargs.get("ambient_C", 25.0))

        trace = {"t_s": [], "pack_V": [], "temp_C": [], "soc": []}
        current = 3.0 * p.capacity_Ah
        t = 0.0
        while t < 3000:
            v = pack.step(current, kwargs.get("ambient_C", 25.0), 1.0)
            trace["t_s"].append(t)
            trace["pack_V"].append(v)
            trace["temp_C"].append(pack.max_temp)
            trace["soc"].append(pack.soc * 100)
            if pack.max_temp > 120 or v <= 8.4:
                break
            t += 1.0
        trace["peak_temp_C"] = max(trace["temp_C"])
        return trace, p

    raise ValueError(f"unknown fault type: {fault_type}")


# ---------------------------------------------------------------
# Calibration from a pulse test
# ---------------------------------------------------------------

def calibrate_r0(voltage_before, voltage_after, current_step_A):
    """R0 from a current pulse.

    Procedure, about 20 minutes:
      1. Let the pack rest 10 minutes at a known SOC. Record voltage.
      2. Apply a step load (e.g. 1A) via the dashboard slider.
      3. Record the voltage IMMEDIATELY (within ~100 ms).
      4. R0 = (V_before - V_after) / current_step

    The instantaneous drop is R0. The slower sag over the next seconds
    is the RC branch. Do this once per cycle and watch R0 climb — that
    is your degradation signal, and it needs no ML at all.
    """
    if current_step_A == 0:
        return None
    return abs(voltage_before - voltage_after) / abs(current_step_A)


def estimate_rc(voltage_trace, dt_s, r0, current_step_A):
    """Rough R1/C1 from the relaxation curve after a pulse.

    Pass the voltage samples recorded in the seconds after the step.
    """
    if len(voltage_trace) < 10:
        return None, None

    v_inst = voltage_trace[0]
    v_settled = voltage_trace[-1]
    v_rc_total = abs(v_settled - v_inst)

    r1 = v_rc_total / abs(current_step_A) if current_step_A else 0.0

    # tau = time to reach 63% of the total change
    target = v_inst + (v_settled - v_inst) * 0.632
    tau = None
    for i, v in enumerate(voltage_trace):
        if (v_settled > v_inst and v >= target) or \
           (v_settled < v_inst and v <= target):
            tau = i * dt_s
            break

    c1 = (tau / r1) if (tau and r1 > 0) else None
    return r1, c1


# ---------------------------------------------------------------
# Quick self-test
# ---------------------------------------------------------------

if __name__ == "__main__":
    print("Thevenin battery model — self test\n")

    for c_rate in (0.5, 1.0, 2.0, 3.0):
        r = simulate_discharge(c_rate=c_rate, ambient_C=25.0)
        print(f"  {c_rate:.1f}C at 25C : "
              f"{r['runtime_min']:5.1f} min, "
              f"peak {r['peak_temp_C']:5.1f}C, "
              f"delivered {r['delivered_Ah']:.2f} Ah")

    print()
    for amb in (10, 25, 45):
        r = simulate_discharge(c_rate=2.0, ambient_C=amb)
        print(f"  2.0C at {amb}C : peak {r['peak_temp_C']:5.1f}C")

    print()
    for soh in (1.0, 0.9, 0.8, 0.7):
        r = simulate_discharge(c_rate=1.0, soh=soh)
        print(f"  SOH {soh*100:3.0f}% : "
              f"{r['runtime_min']:5.1f} min runtime, "
              f"{r['delivered_Ah']:.2f} Ah")
