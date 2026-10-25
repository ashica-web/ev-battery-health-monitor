# Sensors, parameters and digital twin

> Written for the September 2026 expo prototype, which used two ESP32s, the INMP441 microphone and analog voltage taps. The current build uses a single ESP32 and an ADS1115. See the main [README](../README.md) for the current setup.

## 1. Sensors

The system watches the pack from independent physical dimensions: electrical, thermal, mechanical, chemical and acoustic.

| Sensor | Model | Protocol | Qty |
|---|---|---|---|
| Cell temperature | DS18B20 | OneWire | 3 |
| Ambient temperature / humidity | DHT11 | Single wire | 1 |
| Current | ACS712 | Analog | 1 |
| Cell voltage taps | Resistor divider | Analog ADC | 3 |
| Swelling | PZT 27 mm piezo + LM358 | Analog | 2 |
| Acoustic | INMP441 MEMS | I2S | 1 |
| Gas | MQ-2 | Analog | 1 |

### DS18B20 cell temperature × 3
Digital OneWire probes, ±0.5 °C, one bonded to the centre of each cell with thermal paste and glue. All share one data bus.
**Measures:** per-cell temperature, temperature gradient (hottest − coolest), dT/dt (thermal-runaway precursor), thermal asymmetry.

### DHT11 ambient
Temperature (±2 °C) and humidity (±5 %), mounted away from the cells. Lets the model tell internal heating from a hot environment.
**Measures:** ambient temperature, relative humidity.

### ACS712 current
Hall-effect sensor in series with the pack; output centred at VCC/2. Sensitivity: 5 A = 185 mV/A, 20 A = 100 mV/A, 30 A = 66 mV/A. Output scaled to 3.3 V through a 10k/22k divider.
**Measures:** current (+ charge, − discharge), power, coulomb-counting SOC, C-rate.

### Cell voltage taps × 3
Wires at each cell junction through resistor dividers, with 64-sample averaging.
- Cell 1 = Tap 1
- Cell 2 = Tap 2 − Tap 1
- Cell 3 = Tap 3 − Tap 2
- Pack = Tap 3

**Measures:** cell and pack voltage, cell imbalance, over-voltage (> 4.25 V/cell), under-voltage (< 2.8 V/cell).

### Piezo + LM358 × 2
27 mm ceramic discs on the cell surfaces with coupling gel; LM358 non-inverting gain = 1 + 47k/1k = 48.
**Measures:** pressure baseline, swelling events per cycle, expansion trend, mechanical degradation independent of electrical signals.

### INMP441 microphone
I2S digital mic near the cells (on a separate ESP32 in the expo build, due to an I2S/ADC conflict in the Arduino core).
**Measures:** RMS over 256 samples; Normal < 300, Warning 300–700, Danger > 700; acoustic event rate and trend.

### MQ-2 gas
5 V semiconductor sensor near the venting area, 2–3 min warm-up, scaled to 3.3 V.
**Measures:** gas level (ADC 0–4095), alert above 2500, electrolyte-vapour early warning, overcharge hydrogen off-gassing.

## 2. Calculated parameters

| Parameter | How | Why |
|---|---|---|
| SOC | Coulomb counting / capacity | Remaining charge |
| SOH | Current capacity / initial capacity | Overall health |
| RUL | Cycles until SOH < 80 % | Replacement timing |
| Pack voltage | Sum of cells | Pack health |
| Cell imbalance | Max − min cell voltage | Uneven degradation |
| Power | V × \|I\| | Instantaneous power |
| R0 | ΔV / ΔI during a pulse | Best single ageing indicator |
| Capacity per cycle | ∫ discharge current dt | Capacity fade |
| Temperature gradient | Max − min cell temp | Cooling / imbalance issues |
| dT/dt | Temperature rise rate | Thermal-runaway precursor |
| Acoustic RMS | RMS of mic samples | Emission activity |
| Swelling events | Piezo threshold crossings | Mechanical degradation |
| Second-life score | Weighted SOH, R0, thermal, acoustic | Grid-storage reuse, 0–100 |
| Degradation mode | ICA of dV/dQ peaks | SEI growth, lithium plating or active-material loss |

## 3. Digital twin

A live software replica of the pack that:

1. **Mirrors** every measurement within about a second,
2. **Predicts** future state (SOH trend and RUL), and
3. **Explains** what is driving a drop in health (e.g. R0 up from baseline, capacity fading over recent cycles).

### Second-life assessment
When SOH falls below 80 % a cell is retired from EV use but may still serve as grid storage. The score combines remaining capacity, R0 vs. baseline, thermal stability and swelling rate. Above 60: suitable for reuse; below 40: recycle. Aligned with the battery-passport requirements of the EU Battery Regulation.

## 4. SDG alignment

- **SDG 7** Affordable and clean energy
- **SDG 9** Industry, innovation and infrastructure
- **SDG 11** Sustainable cities and communities
- **SDG 12** Responsible consumption and production
- **SDG 13** Climate action
