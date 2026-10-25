"""
simulator.py
============
What-if simulation page. Run scenarios you would never risk on real cells.

This is the capability a dashboard cannot have: asking questions about
the physical system without touching it.

Run:  streamlit run simulator.py
"""

import pandas as pd
import streamlit as st

from battery_model import simulate_discharge, simulate_fault, CellParams

st.set_page_config(page_title="Battery Simulator", layout="wide")

st.markdown("""
<style>
  .stApp { background:#ffffff; color:#24292e; }
  h1,h2,h3 { color:#16181d !important; }
  [data-testid="stMetric"] {
      background:#ffffff; border:1px solid #dcdfe3;
      border-radius:3px; padding:14px 16px;
  }
  [data-testid="stMetricLabel"] p {
      color:#5b616b !important; font-size:13px !important;
  }
</style>
""", unsafe_allow_html=True)

st.title("What-if Simulator")
st.caption("Physics model — Thevenin equivalent circuit with thermal "
           "coupling. Every scenario below runs on the model, not on "
           "the physical pack.")

tab1, tab2, tab3 = st.tabs(["Discharge scenarios",
                            "Compare conditions",
                            "Fault injection"])


# ---------------------------------------------------------------
with tab1:
    st.subheader("Single scenario")

    c1, c2, c3, c4 = st.columns(4)
    c_rate  = c1.slider("Discharge rate (C)", 0.2, 5.0, 1.0, 0.1)
    ambient = c2.slider("Ambient temperature (°C)", -10, 50, 25)
    soh     = c3.slider("State of health (%)", 50, 100, 100) / 100.0
    start   = c4.slider("Starting SOC (%)", 10, 100, 100) / 100.0

    r = simulate_discharge(c_rate=c_rate, ambient_C=ambient,
                           soh=soh, start_soc=start)

    m = st.columns(4)
    m[0].metric("Runtime", f"{r['runtime_min']:.1f} min")
    m[1].metric("Delivered", f"{r['delivered_Ah']:.2f} Ah")
    m[2].metric("Peak temperature", f"{r['peak_temp_C']:.1f} °C")
    m[3].metric("Current", f"{r['current_A'][0]:.2f} A")

    if r["peak_temp_C"] > 55:
        st.error(f"Predicted peak of {r['peak_temp_C']:.1f} °C exceeds "
                 f"the 55 °C safety limit. This scenario would trip the "
                 f"thermal cutoff on the real pack.")
    elif r["peak_temp_C"] > 45:
        st.warning(f"Predicted peak of {r['peak_temp_C']:.1f} °C is high. "
                   f"Sustained operation here would accelerate ageing.")

    df = pd.DataFrame({
        "minutes": [t / 60 for t in r["t_s"]],
        "Pack voltage (V)": r["pack_V"],
        "Temperature (°C)": r["temp_C"],
        "SOC (%)": r["soc"],
    }).set_index("minutes")

    g1, g2 = st.columns(2)
    with g1:
        st.caption("Pack voltage")
        st.line_chart(df[["Pack voltage (V)"]], height=260)
    with g2:
        st.caption("Cell temperature")
        st.line_chart(df[["Temperature (°C)"]], height=260)

    st.caption("State of charge")
    st.line_chart(df[["SOC (%)"]], height=200)


# ---------------------------------------------------------------
with tab2:
    st.subheader("Compare across conditions")

    mode = st.radio("Vary", ["Discharge rate", "Ambient temperature",
                             "State of health"], horizontal=True)

    base_c   = st.slider("Base discharge rate (C)", 0.2, 5.0, 1.0, 0.1,
                         key="cmp_c")
    base_amb = st.slider("Base ambient (°C)", -10, 50, 25, key="cmp_a")

    volt_traces, temp_traces, rows = {}, {}, []

    if mode == "Discharge rate":
        values = [0.5, 1.0, 2.0, 3.0]
        for v in values:
            r = simulate_discharge(c_rate=v, ambient_C=base_amb)
            label = f"{v}C"
            volt_traces[label] = pd.Series(r["pack_V"],
                                           index=[t/60 for t in r["t_s"]])
            temp_traces[label] = pd.Series(r["temp_C"],
                                           index=[t/60 for t in r["t_s"]])
            rows.append({"Scenario": label,
                         "Runtime (min)": round(r["runtime_min"], 1),
                         "Delivered (Ah)": round(r["delivered_Ah"], 2),
                         "Peak temp (°C)": round(r["peak_temp_C"], 1)})

    elif mode == "Ambient temperature":
        values = [0, 15, 25, 40]
        for v in values:
            r = simulate_discharge(c_rate=base_c, ambient_C=v)
            label = f"{v}°C"
            volt_traces[label] = pd.Series(r["pack_V"],
                                           index=[t/60 for t in r["t_s"]])
            temp_traces[label] = pd.Series(r["temp_C"],
                                           index=[t/60 for t in r["t_s"]])
            rows.append({"Scenario": label,
                         "Runtime (min)": round(r["runtime_min"], 1),
                         "Delivered (Ah)": round(r["delivered_Ah"], 2),
                         "Peak temp (°C)": round(r["peak_temp_C"], 1)})

    else:
        values = [1.0, 0.9, 0.8, 0.7]
        for v in values:
            r = simulate_discharge(c_rate=base_c, ambient_C=base_amb, soh=v)
            label = f"SOH {int(v*100)}%"
            volt_traces[label] = pd.Series(r["pack_V"],
                                           index=[t/60 for t in r["t_s"]])
            temp_traces[label] = pd.Series(r["temp_C"],
                                           index=[t/60 for t in r["t_s"]])
            rows.append({"Scenario": label,
                         "Runtime (min)": round(r["runtime_min"], 1),
                         "Delivered (Ah)": round(r["delivered_Ah"], 2),
                         "Peak temp (°C)": round(r["peak_temp_C"], 1)})

    st.dataframe(pd.DataFrame(rows), use_container_width=True,
                 hide_index=True)

    g1, g2 = st.columns(2)
    with g1:
        st.caption("Pack voltage (minutes)")
        st.line_chart(pd.DataFrame(volt_traces), height=280)
    with g2:
        st.caption("Cell temperature (minutes)")
        st.line_chart(pd.DataFrame(temp_traces), height=280)


# ---------------------------------------------------------------
with tab3:
    st.subheader("Fault injection")
    st.caption("Inject a fault into the model to see what it would look "
               "like on the dashboard — without damaging a real cell.")

    fault = st.selectbox("Fault", [
        "high_resistance",
        "capacity_fade",
        "thermal_runaway",
    ], format_func=lambda s: {
        "high_resistance": "Elevated internal resistance (aged cell)",
        "capacity_fade":   "Capacity fade to 70% SOH",
        "thermal_runaway": "Cooling failure under 3C load",
    }[s])

    if st.button("Run fault simulation", type="primary"):
        if fault == "thermal_runaway":
            trace, params = simulate_fault(fault, ambient_C=25.0)

            m = st.columns(2)
            m[0].metric("Peak temperature", f"{trace['peak_temp_C']:.1f} °C")
            m[1].metric("Time to limit", f"{trace['t_s'][-1]/60:.1f} min")

            st.error("With convective cooling reduced to near zero, the "
                     "model predicts the pack crosses its thermal limit. "
                     "This is why dT/dt is monitored as a runaway "
                     "precursor rather than temperature alone.")

            df = pd.DataFrame({
                "minutes": [t/60 for t in trace["t_s"]],
                "Temperature (°C)": trace["temp_C"],
                "Pack voltage (V)": trace["pack_V"],
            }).set_index("minutes")

            st.line_chart(df[["Temperature (°C)"]], height=280)
            st.line_chart(df[["Pack voltage (V)"]], height=220)

        else:
            healthy = simulate_discharge(c_rate=1.0, ambient_C=25.0)
            faulty, params = simulate_fault(fault, c_rate=1.0,
                                            ambient_C=25.0)

            m = st.columns(3)
            m[0].metric("Healthy runtime",
                        f"{healthy['runtime_min']:.1f} min")
            m[1].metric("Faulty runtime",
                        f"{faulty['runtime_min']:.1f} min",
                        delta=f"{faulty['runtime_min'] - healthy['runtime_min']:.1f} min")
            m[2].metric("Peak temp difference",
                        f"{faulty['peak_temp_C'] - healthy['peak_temp_C']:+.1f} °C")

            comp_v = pd.DataFrame({
                "Healthy": pd.Series(healthy["pack_V"],
                                     index=[t/60 for t in healthy["t_s"]]),
                "Faulty": pd.Series(faulty["pack_V"],
                                    index=[t/60 for t in faulty["t_s"]]),
            })
            comp_t = pd.DataFrame({
                "Healthy": pd.Series(healthy["temp_C"],
                                     index=[t/60 for t in healthy["t_s"]]),
                "Faulty": pd.Series(faulty["temp_C"],
                                    index=[t/60 for t in faulty["t_s"]]),
            })

            g1, g2 = st.columns(2)
            with g1:
                st.caption("Pack voltage — healthy vs faulty")
                st.line_chart(comp_v, height=280)
            with g2:
                st.caption("Temperature — healthy vs faulty")
                st.line_chart(comp_t, height=280)


st.divider()
st.caption("Model parameters are defaults for a healthy 18650. "
           "Calibrate R0 with a pulse test once the current sensor is "
           "wired — see calibrate_r0() in battery_model.py.")
