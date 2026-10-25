# Why each component is on the board (ev_bms rev A.1)

The board has four blocks: **(1) battery monitor**, which measures every cell, the pack current and the cell temperatures;
**(2) power supply**, which makes 3.3 V from the pack; **(3) MCU**, which reads everything, runs the AI model and sends the data out;
**(4) safety sensors**, which catch the early signs of thermal runaway (gas, sound, swelling).

---

## 1. Battery monitor — BQ76952 analog front end (AFE)

| Ref | Part | Why it is there |
|---|---|---|
| **U1** | TI BQ76952 | Measures all 3–16 series cells. The cells are stacked up to 67 V, but each one has to be read to a few mV. The ESP32 ADC can't do this: it only reads 0–3.3 V against its own ground, it is non-linear, and a resistor divider on the top cell would divide 67 V and lose the accuracy. The BQ76952 level-shifts each cell internally and also does **current measurement (coulomb counter)**, **temperature measurement**, **cell balancing**, and **hardware protection** (over/under-voltage, over-current, short circuit, over-temperature). Those protections trip inside the IC in microseconds to milliseconds, even if the ESP32 has crashed. It talks to the ESP32 over I2C (address 0x08). |
| **J1** | 16-pin header | One sense wire from the + terminal of every cell. Cell 1's − terminal is BAT− on J2. |
| **R1–R17** | 20 Ω, 1206 | Series resistor on each cell input. With C1–C17 it forms a low-pass filter (TI EVM values) that keeps switching noise and wiring spikes out of the ADC. It also limits the current into the IC pins when a cell connector is plugged in live, and it sets the passive balancing current. The 1206 size handles that balancing heat. |
| **C1–C17** | 220 nF 50 V | The capacitor half of the filter. Each one sits across one cell (cell-to-cell), so it only ever sees one cell's voltage. |
| **JP1–JP13** | Solder jumpers | Make the same board work for 3S to 16S. TI requires unused inputs to be shorted to the input below, and VC0–VC1, VC1–VC2 and VC15–VC16 to always be real cells. Closing jumpers removes the cells you don't have (3S = close all 13). |
| **D1** | BAT46W diode (100 V) | Feeds the AFE supply from the top of the stack. If the pack is shorted and the stack voltage collapses, the diode stops C18 from discharging backwards, so the AFE stays powered long enough to detect the short and switch off. |
| **R18 + C18** | 100 Ω + 1 µF 100 V | RC filter on the AFE supply pin. Removes load transients from the pack. Rated 100 V because it sees the full pack voltage. |
| **C19** | 2.2 µF | Required capacitor for the AFE's internal 1.8 V regulator (REG18). |
| **RS1** | 2 mΩ shunt, 3 W 2512 | All pack current flows through it. 10 A gives 20 mV, which the AFE's coulomb counter measures. This gives exact current, state-of-charge, and over-current/short detection. The ±200 mV input range gives ±100 A of range with 2 mΩ. At 5 A it only dissipates 0.05 W. |
| **NT1, NT2** | Net ties (Kelvin) | The current-sense traces start right at the shunt pads, so they measure only the drop across the shunt and not the drop along the thick power copper. Without them, the copper's resistance would show up as a current error. |
| **R19, R20 + C20** | 100 Ω, 100 Ω + 100 nF | Filter on the current-sense inputs (SRP/SRN), per TI. Removes switching noise so the coulomb counter integrates the real current. |
| **J3** | 6-pin header | Up to five 10 k NTC thermistors, glued to the cells. The AFE reads them directly (TS1, TS3, HDQ, DCHG, DDSG pins) and can trip over-temperature in hardware. |
| **R21** | 10 kΩ | Pull-up for the AFE's ALERT output (open-drain). ALERT interrupts the ESP32 the moment a fault or new measurement occurs, so the ESP32 doesn't have to keep polling. |
| **R22** | 100 kΩ | Pull-down on RST_SHUT. While the ESP32 is booting its pins float, and this resistor stops the AFE from being reset or shut down by accident. |
| **SW1** | WAKE button | The BQ76952 ships in SHUTDOWN mode (it draws almost nothing). Pressing the button pulls TS2 low, which wakes it. |
| **J2** | 3-way terminal block | PACK+ (top of stack), BAT− (cell 1 negative = board GND), PACK− (load/charger side of the shunt). |

## 2. Power supply — 3.3 V from the pack

| Ref | Part | Why it is there |
|---|---|---|
| **U2** | TI LM5164 buck (100 V) | Makes 3.3 V from anything between ≈7 V (3S) and 67 V (16S). A linear regulator would burn (67 − 3.3 V) × 0.15 A ≈ 9.5 W at 16S. A buck converter is ~85–90 % efficient. The 100 V rating gives margin above the 67 V of a full 16S pack. MOSFETs are built in, so it needs few parts. |
| **F1** | 0.5 A 125 V fuse | If the converter ever fails short, the fuse opens instead of the pack driving current into a dead short. Rated 125 V, above the pack voltage. |
| **C21, C22** | 2.2 µF 100 V, 1210 | Input capacitors. They supply the pulsed current the buck switch draws, so the pack wires don't have to. 100 V parts, because ceramic capacitors lose capacitance as their DC voltage rises. |
| **R23 + R24** | 1 MΩ / 274 kΩ | Under-voltage lock-out. The buck only starts above ≈7 V, so it can't brown-out and chatter on an empty pack. |
| **R25** | 27.4 kΩ | Sets the on-time, which gives ≈300 kHz switching (the LM5164 keeps that frequency roughly constant across input voltage). |
| **C23** | 2.2 nF | Bootstrap capacitor. It powers the high-side gate driver, which sits above the switch node. |
| **L1** | 33 µH 2 A, shielded | The buck's energy-storage inductor. It is shielded so its magnetic field doesn't couple into the AFE inputs or the ESP32 antenna. |
| **R26 / R27** | 174 k / 100 k | Feedback divider: 1.2 V × (1 + 174/100) = 3.29 V. |
| **R28, C24, C25** | 158 k / 3.3 nF / 150 pF | Ripple injection. This kind of regulator (constant on-time) needs a small ripple at its feedback pin to switch cleanly. Ceramic output capacitors are too good to give one, so this RC network creates it from the switch node. |
| **C26, C27** | 22 µF ×2 | Output capacitors. They keep 3.3 V steady during the ESP32's Wi-Fi transmit bursts (≈350 mA peaks). |

## 3. MCU — reads, decides, communicates

| Ref | Part | Why it is there |
|---|---|---|
| **U3** | ESP32-S3-WROOM-1 (N8) | The brain. It reads the AFE and sensors over I2C, runs the AI/anomaly model (the S3 has vector instructions for ML), and sends data over Wi-Fi/BLE to the dashboard/digital twin. It reads the microphone over I2S and gets programmed over native USB. One S3 replaces the prototype's two ESP32s. It is a certified module with a built-in antenna, so no RF design is needed. The antenna sits at the board edge with no copper under it. |
| **C28, C29** | 10 µF + 100 nF | Decoupling right at the module's 3.3 V pin for the Wi-Fi current spikes. |
| **R29 + C30** | 10 k + 1 µF | Power-on delay on EN. The ESP32 must start only after 3.3 V is stable (Espressif requirement). |
| **SW2, SW3, R30** | RESET, BOOT, 10 k | Hold BOOT and tap RESET to force download mode. This recovers the board if bad firmware breaks USB. R30 keeps BOOT high for normal start-up. |
| **R31, R32** | 4.7 kΩ | I2C pull-ups for the AFE, SHT40, SGP41 and J7 bus. I2C lines are open-drain and need them. |
| **R33 + D2** | 1 k + green LED | Status/alarm indicator. Useful at the demo even without the dashboard. |
| **J4** | USB-C (GCT USB4085) | Programming and serial log. Through-hole shell pins make it mechanically strong. |
| **U5** | USBLC6-2SC6 | ESD protection on the USB data lines. A finger on the connector can carry several kV, and this clamps it before it reaches the ESP32. |
| **R34, R35** | 5.1 kΩ | USB-C CC pull-downs. Without them a USB-C host or C-to-C cable doesn't recognise the board as a device. |
| **J8** | 3-pin UART | Backup programming/debug console. |

## 4. Safety sensors — catch failures before they become fires

| Ref | Part | Why it is there |
|---|---|---|
| **U6** | Sensirion SHT40 | Ambient temperature + humidity inside the pack. Humidity means condensation/corrosion risk. Ambient temperature is the reference: a cell NTC much hotter than ambient points to an internal fault, not hot weather. ±0.2 °C, compared with ±2 °C for the prototype's DHT11. |
| **U7** | Sensirion SGP41 | VOC + NOx gas sensor. A failing cell vents electrolyte vapour before its temperature runs away, so gas is the earliest warning. It replaces the MQ-2, which needs a 5 V heater at ~150 mA, a long burn-in and is barely selective. The SGP41 is 3.3 V, I2C and low power. |
| **R36 + C32** | 10 Ω + 1 µF | Filter on the SGP41's VDD so its hotplate pulses don't disturb the sensor's measurement supply. |
| **C31, C33** | 100 nF, 1 µF | Decoupling for SHT40 and SGP41 (VDDH). |
| **J5** | Mic pod header (I2S) | INMP441 microphone on a short cable, placed next to the cells. It listens for the hiss/click of a cell venting. |
| **J6** | Swelling pod header | HX711 + load cell/strain gauge (replaces piezo + LM358). It measures cell swelling, which grows as a cell degrades. |
| **J7** | I2C expansion | Add more sensors (e.g. a second NTC board, display) without a new PCB. |

## Board itself

| Item | Why |
|---|---|
| **4 layers, In1 = solid GND plane** | The AFE measures µV-level shunt signals and the ESP32 transmits RF. A continuous ground plane under everything keeps return currents short and noise low. It is the single biggest difference from a 2-layer hobby board. |
| **H1–H4** | M3 mounting holes for the enclosure. |
| **Antenna keep-out** | No copper on any layer under the ESP32 antenna, otherwise Wi-Fi range drops. |

## Prototype part → product part

| Prototype | Product board | Reason |
|---|---|---|
| Resistor dividers into ESP32 ADC | BQ76952 AFE | mV accuracy on a 67 V stack, plus hardware protection |
| ACS712 Hall sensor | 2 mΩ shunt + AFE coulomb counter | Less noise and offset drift. Integrated current gives state-of-charge. |
| DS18B20 ×3 | 10 k NTC ×5 on the AFE | The AFE reads them directly and trips over-temperature in hardware |
| DHT11 | SHT40 | ±0.2 °C / ±1.8 %RH instead of ±2 °C / ±5 %RH |
| MQ-2 | SGP41 | 3.3 V, low power, digital, made for VOCs |
| Piezo + LM358 ×2 | HX711 swelling pod | Calibrated force/strain reading instead of an analog spike |
| INMP441 | INMP441 (kept, on J5) | Already digital (I2S). Kept. |
| 2 × ESP32 | 1 × ESP32-S3 | Enough I/O and compute for everything, so one firmware and one radio |
