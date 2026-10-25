"""
kalman_soc.py - Extended Kalman Filter for state of charge.

WHY
  Coulomb counting (adding up current x time) is what the firmware uses.
  It is smooth but it DRIFTS: a 0.05 A sensor offset becomes a 5% SOC
  error after two hours, and a wrong starting SOC is never corrected
  until the pack rests. Voltage alone can't replace it either - under
  load it sags and it is noisy.

  The Kalman filter uses both, every second:
    1. PREDICT  with the physics model: "after this current, SOC is X"
    2. CORRECT  with the measured voltage: "the voltage says X is a bit
                high" - and nudge SOC by an amount that depends on how
                much each source is trusted right now
  It also reports its own uncertainty, so you get "SOC 63% +/- 1.5%".

MODEL (one cell of the series pack - the Thevenin model in battery_model.py)
  state      x = [SOC, V_rc]
  predict    SOC  <- SOC - I*dt / (3600 * capacity)
             V_rc <- a*V_rc + (1-a)*R1*I,          a = exp(-dt / (R1*C1))
  measure    V    =  OCV(SOC) - I*R0 - V_rc
  "Extended" because OCV(SOC) is a curve, so the filter uses its local
  slope (dOCV/dSOC) at the current estimate.

  I is positive when DISCHARGING (the firmware's current_A is negative
  then, so callers pass -current_A).
"""

import math

from battery_model import ocv


def docv_dsoc(soc, h=0.005):
    lo, hi = max(0.0, soc - h), min(1.0, soc + h)
    return (ocv(hi) - ocv(lo)) / (hi - lo)


class SocEKF:
    """
    Three hidden quantities, estimated together every second:
        SOC    state of charge
        V_rc   polarisation voltage (the slow part of the voltage sag)
        R0     internal resistance - learned live, so an aged cell does
               not bias the SOC, and R0 itself is an ageing indicator
    """

    def __init__(self, params,
                 current_noise_A=0.3,      # current sensor error incl. offset drift
                 voltage_noise_V=0.05,     # voltage sensor + model error, per cell
                 vrc_noise_V=0.002,
                 r0_drift_ohm=2e-4):       # how fast R0 may change per step
        self.p = params
        self.sig_i = current_noise_A
        self.R = voltage_noise_V ** 2
        self.q_vrc = vrc_noise_V ** 2
        self.q_r0 = r0_drift_ohm ** 2
        self.ready = False
        self.x = [1.0, 0.0, params.R0_aged]
        self.P = [[1.0, 0, 0], [0, 0, 0], [0, 0, 0]]
        self.innovation = 0.0
        self.predicted_V = 0.0

    @property
    def soc(self):
        return self.x[0]

    @property
    def r0(self):
        return self.x[2]

    @property
    def sigma(self):
        """One standard deviation of the SOC estimate (fraction)."""
        return math.sqrt(max(self.P[0][0], 0.0))

    # ------------------------------------------------------------------
    def start(self, cell_voltage, soc_guess=None):
        """Initialise from a voltage reading (best at rest)."""
        from battery_model import soc_from_ocv
        soc = soc_from_ocv(cell_voltage) if soc_guess is None else soc_guess
        self.x = [soc, 0.0, self.p.R0_aged]
        # Not very sure at the start: +/-10% SOC, +/-50% R0
        self.P = [[0.10 ** 2, 0, 0],
                  [0, 0.02 ** 2, 0],
                  [0, 0, (0.5 * self.p.R0_aged) ** 2]]
        self.ready = True

    # ------------------------------------------------------------------
    def step(self, current_A, cell_voltage, dt):
        """current_A: + discharging. cell_voltage: measured, per cell."""
        if not self.ready:
            self.start(cell_voltage)
            return self.soc

        p = self.p
        cap_As = 3600.0 * p.capacity_aged
        tau = p.R1_ohm * p.C1_farad
        a = math.exp(-dt / tau) if tau > 0 else 0.0
        soc, vrc, r0 = self.x

        # ---------- 1. predict ----------
        xp = [min(1.0, max(0.0, soc - current_A * dt / cap_As)),
              a * vrc + (1.0 - a) * p.R1_ohm * current_A,
              r0]
        F = [1.0, a, 1.0]                               # diagonal
        Q = [(self.sig_i * dt / cap_As) ** 2, self.q_vrc, self.q_r0]
        P = self.P
        Pp = [[F[i] * P[i][j] * F[j] + (Q[i] if i == j else 0.0)
               for j in range(3)] for i in range(3)]

        # ---------- 2. correct with the measured voltage ----------
        v_pred = ocv(xp[0]) - current_A * xp[2] - xp[1]
        H = [docv_dsoc(xp[0]), -1.0, -current_A]
        y = cell_voltage - v_pred                       # innovation
        PHt = [sum(Pp[i][k] * H[k] for k in range(3)) for i in range(3)]
        S = sum(H[i] * PHt[i] for i in range(3)) + self.R
        K = [PHt[i] / S for i in range(3)]              # Kalman gain

        self.x = [min(1.0, max(0.0, xp[0] + K[0] * y)),
                  xp[1] + K[1] * y,
                  min(0.5, max(0.005, xp[2] + K[2] * y))]
        newP = [[Pp[i][j] - K[i] * PHt[j] for j in range(3)] for i in range(3)]
        for i in range(3):                              # keep it symmetric, positive
            newP[i][i] = max(newP[i][i], 1e-12)
            for j in range(i):
                m = 0.5 * (newP[i][j] + newP[j][i])
                newP[i][j] = newP[j][i] = m
        self.P = newP

        self.innovation = y
        self.predicted_V = v_pred
        return self.soc
