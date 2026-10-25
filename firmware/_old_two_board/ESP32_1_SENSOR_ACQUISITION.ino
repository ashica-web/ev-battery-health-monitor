/*
=========================================================
ESP32 #1 - SENSOR ACQUISITION, SOC & SOH
=========================================================
REVISION 4.1 - real-time SOC and SOH, safety cutoff reports its reason

HOW SOC WORKS NOW
  * At rest the pack's voltage tells you its SOC (OCV lookup table).
  * Under load, coulomb counting tracks it second by second.
  * SOC is synced from voltage:
      - as soon as the first plausible pack voltage appears
        (works even if the ESP32 boots before the pack is connected)
      - again after every 10 minutes at rest, cancelling drift
  * Implausible voltage (wiring fault) never overwrites SOC.

HOW SOH WORKS NOW
  SOH changes slowly - over cycles, not seconds - so it is measured
  two ways and the best available one is reported:
    1. RESISTANCE: every time the load steps by >0.3 A, internal
       resistance R0 = dV/dI is measured. Rising R0 is the strongest
       ageing indicator. Available within seconds of the first load.
    2. CAPACITY:   after a full discharge + CYCLE command, the measured
       capacity vs the first cycle. The gold standard, but needs cycles.
  "soh_source" in the JSON says which one is in use:
       assumed  -> nothing measured yet (shows 100)
       r0       -> resistance-based estimate
       capacity -> capacity-based measurement

OTHER CHANGES FROM REV 3
  * Safety-cutoff event sent once per trip, not 5 times a second
  * New JSON fields: soc_source, soh_source, r0_mohm, capacity_Ah,
    cycle_discharge_Ah
  * New command SYNC -> re-sync SOC from voltage now

WIRING NOTE: ESP32 GND must go to the BMS B- terminal, NOT P-.

Board: ESP32 Dev Module
Libraries: OneWire, DallasTemperature, DHT sensor library
*/

#include <OneWire.h>
#include <DallasTemperature.h>
#include <DHT.h>

// ==================== PIN MAP ====================
#define ONE_WIRE_BUS      19   // DS18B20 x3, 4.7k pullup to 3.3V
#define DHT_PIN            2   // DHT11 data
#define MOSFET_PWM_PIN    13   // MOSFET gate via 330R, 10k pulldown

#define CELL_TAP1_PIN     33   // ADC1 - cell 1
#define CELL_TAP2_PIN     32   // ADC1 - cells 1+2
#define CELL_TAP3_PIN     34   // ADC1 - pack total   [input only]
#define CURRENT_PIN       35   // ADC1 - ACS712       [input only]
#define PIEZO_1_PIN       26   // ADC2 - fine here, this board has no WiFi
#define PIEZO_2_PIN       27   // ADC2 - fine here, this board has no WiFi
#define GAS_PIN           25   // ADC2 - MQ2

#define UART_RX_PIN       16
#define UART_TX_PIN       17
#define UART_BAUD     115200

// ==================== PWM (core 2.x / 3.x) ====================
#define PWM_FREQ      5000
#define PWM_RES_BITS     10
#define PWM_MAX       1023

#if ESP_ARDUINO_VERSION_MAJOR >= 3
  #define PWM_CHANNEL MOSFET_PWM_PIN
#else
  #define PWM_CHANNEL 0
#endif

// ==================== CALIBRATION (your measured values) ====================
#define TAP1_FACTOR   3.2727f
#define TAP2_FACTOR   5.5450f
#define TAP3_FACTOR   5.5450f

#define ACS712_DIV_RATIO    0.6667f
#define ACS712_SENS_V_PER_A 0.185f    // 5A variant. 20A=0.100, 30A=0.066

// ==================== PACK CONSTANTS ====================
#define PACK_CAPACITY_MAH  2500.0f
#define CELL_COUNT            3
#define CELL_V_MAX          4.25f
#define CELL_V_MIN          2.80f
#define CELL_TEMP_MAX       55.0f
#define GAS_ALERT_LEVEL     2500

// Plausible range for one real cell - outside this = wiring fault
#define CELL_V_PLAUSIBLE_MIN 2.50f
#define CELL_V_PLAUSIBLE_MAX 4.35f

// Per-cell internal resistance of a NEW cell. Take it from the datasheet,
// or measure it once on fresh cells and put that number here.
#define R0_NEW_MOHM         45.0f

// ==================== TIMING ====================
#define SENSOR_INTERVAL_MS   1000
#define CONTROL_INTERVAL_MS   200
#define ADC_SAMPLES            64
#define REST_RESYNC_MS     600000UL   // 10 min at rest -> re-sync SOC

// ==================== OBJECTS ====================
OneWire oneWire(ONE_WIRE_BUS);
DallasTemperature tempSensors(&oneWire);
DHT dht(DHT_PIN, DHT11);

// ==================== STATE ====================
struct BatteryState {
  float cell_temp[CELL_COUNT];
  float ambient_temp, ambient_humidity;
  float cell_voltage[CELL_COUNT];
  float pack_voltage;
  float current_A;
  float power_W;
  float soc_percent;
  float soh_percent;
  float piezo_raw[2];
  int   gas_level;
  float cell_imbalance;
  float temp_gradient;
  float dT_dt;
  int   cycles_completed;
  bool  fault_overvoltage;
  bool  fault_undervoltage;
  bool  fault_overtemp;
  bool  fault_gas;
};
BatteryState bat;

// ---- Coulomb counting ----
float coulomb_mAh           = 0.0f;   // 0 = full, negative = charge removed
float cycle_discharge_mAh   = 0.0f;
float baseline_capacity_mAh = PACK_CAPACITY_MAH;
float measured_capacity_mAh = PACK_CAPACITY_MAH;

// ---- SOC sync ----
bool          soc_synced = false;
unsigned long rest_since = 0;
const char*   soc_source = "none";    // none / ocv / coulomb

// ---- SOH ----
const char* soh_source = "assumed";   // assumed / r0 / capacity

// ---- R0 measurement ----
float         r0_mohm     = 0.0f;
bool          r0_valid    = false;
bool          r0_pending  = false;
float         r0_v_before = 0.0f;
float         r0_i_before = 0.0f;
unsigned long r0_started  = 0;

// ---- ACS712 ----
float acs712_zero_volts = 0.0f;

// ---- Load control ----
float target_current_A = 0.0f;
int   pwm_duty         = 0;
#define KP_CURRENT   40.0f
#define PWM_STEP_MAX    25

// ---- BMS trip / safety ----
bool bms_tripped = false;
int  zero_current_count = 0;
#define BMS_TRIP_COUNT 5
bool safety_latched = false;
bool safety_active = false;
String safety_reason = "";

// ---- dT/dt ----
float last_max_temp = 0.0f;
unsigned long last_temp_time = 0;

unsigned long last_sensor_ms  = 0;
unsigned long last_control_ms = 0;

// ==================== OCV -> SOC ====================
const float OCV_V[]   = {3.00,3.35,3.45,3.51,3.55,3.62,3.68,3.74,3.82,3.90,3.99,4.09,4.15,4.20};
const float OCV_SOC[] = {0,   5,   10,  15,  20,  30,  40,  50,  60,  70,  80,  90,  95,  100};
const int   OCV_N = 14;

float socFromOcv(float v) {
  if (v <= OCV_V[0]) return 0.0f;
  if (v >= OCV_V[OCV_N - 1]) return 100.0f;
  for (int i = 0; i < OCV_N - 1; i++) {
    if (v >= OCV_V[i] && v <= OCV_V[i + 1]) {
      float f = (v - OCV_V[i]) / (OCV_V[i + 1] - OCV_V[i]);
      return OCV_SOC[i] + f * (OCV_SOC[i + 1] - OCV_SOC[i]);
    }
  }
  return 100.0f;
}

bool packVoltagePlausible() {
  float per_cell = bat.pack_voltage / CELL_COUNT;
  return per_cell >= CELL_V_PLAUSIBLE_MIN && per_cell <= CELL_V_PLAUSIBLE_MAX;
}

// Set the coulomb counter so SOC matches the resting voltage
bool syncSocToOcv() {
  if (!packVoltagePlausible()) return false;
  float soc = socFromOcv(bat.pack_voltage / CELL_COUNT);
  coulomb_mAh = (soc - 100.0f) / 100.0f * PACK_CAPACITY_MAH;
  soc_synced = true;
  soc_source = "ocv";
  Serial.printf("SOC synced from voltage: %.3f V -> %.1f%%\n",
                bat.pack_voltage, soc);
  return true;
}

// ==================== ANALOG HELPERS ====================
float readPinVolts(int pin) {
  uint32_t acc = 0;
  for (int i = 0; i < ADC_SAMPLES; i++) acc += analogRead(pin);
  float raw = (float)acc / ADC_SAMPLES;
  return (raw / 4095.0f) * 3.3f;
}

int readPinRaw(int pin) {
  uint32_t acc = 0;
  for (int i = 0; i < ADC_SAMPLES; i++) acc += analogRead(pin);
  return (int)(acc / ADC_SAMPLES);
}

// ==================== SENSORS ====================
void readTemperatures() {
  tempSensors.requestTemperatures();
  for (int i = 0; i < CELL_COUNT; i++) {
    float t = tempSensors.getTempCByIndex(i);
    if (t != DEVICE_DISCONNECTED_C && t > -50 && t < 150) {
      bat.cell_temp[i] = t;
    }
  }
}

void readAmbient() {
  float t = dht.readTemperature();
  float h = dht.readHumidity();
  if (!isnan(t)) bat.ambient_temp = t;
  if (!isnan(h)) bat.ambient_humidity = h;
}

void readCellVoltages() {
  float v_tap1 = readPinVolts(CELL_TAP1_PIN) * TAP1_FACTOR;
  float v_tap2 = readPinVolts(CELL_TAP2_PIN) * TAP2_FACTOR;
  float v_tap3 = readPinVolts(CELL_TAP3_PIN) * TAP3_FACTOR;

  bat.cell_voltage[0] = v_tap1;
  bat.cell_voltage[1] = v_tap2 - v_tap1;
  bat.cell_voltage[2] = v_tap3 - v_tap2;
  bat.pack_voltage    = v_tap3;
}

float readPackVoltageNow() {
  return readPinVolts(CELL_TAP3_PIN) * TAP3_FACTOR;
}

void readCurrent() {
  float v_sensor = readPinVolts(CURRENT_PIN) / ACS712_DIV_RATIO;
  bat.current_A = (v_sensor - acs712_zero_volts) / ACS712_SENS_V_PER_A;
  if (fabs(bat.current_A) < 0.05f) bat.current_A = 0.0f;
}

void calibrateCurrentZero() {
  ledcWrite(PWM_CHANNEL, 0);
  delay(500);
  float sum = 0;
  for (int i = 0; i < 20; i++) {
    sum += readPinVolts(CURRENT_PIN) / ACS712_DIV_RATIO;
    delay(20);
  }
  acs712_zero_volts = sum / 20.0f;
}

void readPiezo() {
  bat.piezo_raw[0] = readPinRaw(PIEZO_1_PIN);
  bat.piezo_raw[1] = readPinRaw(PIEZO_2_PIN);
}

void readGas() {
  bat.gas_level = readPinRaw(GAS_PIN);
}

// ==================== SOC ====================
void updateCoulombCount() {
  float hours = SENSOR_INTERVAL_MS / 3600000.0f;
  float mAh = bat.current_A * 1000.0f * hours;
  coulomb_mAh += mAh;
  if (mAh < 0) cycle_discharge_mAh += -mAh;   // discharge reads negative
  if (fabs(bat.current_A) > 0.05f && soc_synced) soc_source = "coulomb";
}

void manageSocSync() {
  // First plausible voltage since boot -> sync immediately.
  // Handles the ESP32 booting before the pack is connected.
  if (!soc_synced) {
    if (fabs(bat.current_A) < 0.1f) syncSocToOcv();
    return;
  }

  // After 10 minutes at rest, re-sync to cancel coulomb drift
  if (fabs(bat.current_A) < 0.05f) {
    if (rest_since == 0) {
      rest_since = millis();
    } else if (millis() - rest_since > REST_RESYNC_MS) {
      syncSocToOcv();
      rest_since = millis();
    }
  } else {
    rest_since = 0;
  }
}

void calculateSOC() {
  float soc = 100.0f + (coulomb_mAh / PACK_CAPACITY_MAH) * 100.0f;
  bat.soc_percent = constrain(soc, 0.0f, 100.0f);
}

// ==================== R0 / SOH ====================
// Called when the load target changes. Remembers voltage and current
// just before the step.
void startR0Measurement() {
  float v = readPackVoltageNow();
  if (v / CELL_COUNT < CELL_V_PLAUSIBLE_MIN) return;   // no real pack
  r0_v_before = v;
  r0_i_before = fabs(bat.current_A);
  r0_pending  = true;
  r0_started  = millis();
}

// Called every control tick. Once the current has actually moved by
// 0.3 A, the instantaneous voltage change gives R0.
void checkR0Measurement() {
  if (!r0_pending) return;
  if (millis() - r0_started > 10000UL) { r0_pending = false; return; }

  float i_now = fabs(bat.current_A);
  float di = i_now - r0_i_before;
  if (fabs(di) < 0.3f) return;

  float v_now = readPackVoltageNow();
  float dv = r0_v_before - v_now;            // voltage falls as load rises
  float r_cell = (dv / di) / CELL_COUNT;     // ohms, per cell
  r0_pending = false;

  if (r_cell <= 0.005f || r_cell >= 0.5f) {
    Serial.printf("R0 reading rejected (%.1f mOhm)\n", r_cell * 1000.0f);
    return;
  }

  float m = r_cell * 1000.0f;
  r0_mohm = r0_valid ? (0.7f * r0_mohm + 0.3f * m) : m;   // smoothed
  r0_valid = true;
  Serial.printf("R0 measured %.1f mOhm/cell (smoothed %.1f)\n", m, r0_mohm);
}

void calculateSOH() {
  if (bat.cycles_completed > 0) {
    // Best: measured capacity vs the first cycle
    bat.soh_percent = constrain(
        (measured_capacity_mAh / baseline_capacity_mAh) * 100.0f, 0.0f, 100.0f);
    soh_source = "capacity";
  } else if (r0_valid) {
    // Estimate: R0 roughly doubles by 80% SOH (same relation the
    // digital-twin physics model uses)
    float ratio = r0_mohm / R0_NEW_MOHM;
    bat.soh_percent = constrain(100.0f - 20.0f * (ratio - 1.0f), 0.0f, 100.0f);
    soh_source = "r0";
  } else {
    bat.soh_percent = 100.0f;
    soh_source = "assumed";
  }
}

void markCycleComplete() {
  if (cycle_discharge_mAh > 100) {
    measured_capacity_mAh = cycle_discharge_mAh;
    if (bat.cycles_completed == 0) {
      baseline_capacity_mAh = cycle_discharge_mAh;
    }
    bat.cycles_completed++;
    Serial.printf("*** Cycle %d complete, capacity = %.1f mAh\n",
                  bat.cycles_completed, measured_capacity_mAh);
  } else {
    Serial.println(F("CYCLE ignored - less than 100 mAh discharged"));
  }
  cycle_discharge_mAh = 0;
}

// ==================== DERIVED / FAULTS ====================
void calculateDerived() {
  bat.power_W = bat.pack_voltage * fabs(bat.current_A);

  float vmax = bat.cell_voltage[0], vmin = bat.cell_voltage[0];
  float tmax = bat.cell_temp[0],    tmin = bat.cell_temp[0];
  for (int i = 1; i < CELL_COUNT; i++) {
    vmax = max(vmax, bat.cell_voltage[i]);
    vmin = min(vmin, bat.cell_voltage[i]);
    tmax = max(tmax, bat.cell_temp[i]);
    tmin = min(tmin, bat.cell_temp[i]);
  }
  bat.cell_imbalance = vmax - vmin;
  bat.temp_gradient  = tmax - tmin;

  unsigned long now = millis();
  float dt_s = (now - last_temp_time) / 1000.0f;
  if (dt_s > 0.5f) {
    bat.dT_dt = (tmax - last_max_temp) / dt_s;
    last_max_temp  = tmax;
    last_temp_time = now;
  }
}

void checkFaults() {
  bat.fault_overvoltage  = false;
  bat.fault_undervoltage = false;
  bat.fault_overtemp     = false;

  for (int i = 0; i < CELL_COUNT; i++) {
    if (bat.cell_voltage[i] > CELL_V_MAX)    bat.fault_overvoltage = true;
    if (bat.cell_voltage[i] < CELL_V_MIN)    bat.fault_undervoltage = true;
    if (bat.cell_temp[i]    > CELL_TEMP_MAX) bat.fault_overtemp = true;
  }
  bat.fault_gas = (bat.gas_level > GAS_ALERT_LEVEL);
}

// ==================== LOAD CONTROL ====================
void runCurrentControl() {
  if (target_current_A <= 0.0f) {
    pwm_duty = 0;
    ledcWrite(PWM_CHANNEL, 0);
    zero_current_count = 0;
    return;
  }

  float actual = fabs(bat.current_A);

  if (actual < 0.05f && pwm_duty > 100) {
    zero_current_count++;
    if (zero_current_count >= BMS_TRIP_COUNT && !bms_tripped) {
      bms_tripped = true;
      pwm_duty = 0;
      ledcWrite(PWM_CHANNEL, 0);
      Serial2.println(F("{\"event\":\"BMS_TRIP\"}"));
      Serial.println(F("*** BMS CUTOFF DETECTED - load released ***"));
    }
    return;
  }

  zero_current_count = 0;
  if (actual > 0.1f) bms_tripped = false;
  if (bms_tripped) return;

  float error = target_current_A - actual;
  int correction = constrain((int)(error * KP_CURRENT),
                             -PWM_STEP_MAX, PWM_STEP_MAX);
  pwm_duty = constrain(pwm_duty + correction, 0, PWM_MAX);
  ledcWrite(PWM_CHANNEL, pwm_duty);
}

// Firmware backstop. NOT a substitute for the BMS and a fuse.
void checkSafetyCutoff() {
  String reason = "";

  for (int i = 0; i < CELL_COUNT; i++) {
    if (bat.cell_voltage[i] < CELL_V_MIN && bat.cell_voltage[i] > 0.5f) {
      reason = "cell" + String(i + 1) + " undervoltage " +
               String(bat.cell_voltage[i], 2) + "V";
      break;
    }
    if (bat.cell_temp[i] > CELL_TEMP_MAX) {
      reason = "cell" + String(i + 1) + " overtemp " +
               String(bat.cell_temp[i], 1) + "C";
      break;
    }
  }
  if (reason.length() == 0 && bat.gas_level > GAS_ALERT_LEVEL)
    reason = "gas " + String(bat.gas_level);
  if (reason.length() == 0 && bat.dT_dt > 1.0f)
    reason = "dT/dt " + String(bat.dT_dt, 2) + "C/s";

  if (reason.length() > 0) {
    target_current_A = 0.0f;
    pwm_duty = 0;
    ledcWrite(PWM_CHANNEL, 0);
    safety_active = true;
    if (!safety_latched || reason != safety_reason) {   // report once per cause
      Serial2.println("{\"event\":\"SAFETY_CUTOFF\",\"reason\":\"" + reason + "\"}");
      Serial.println("!!! SAFETY CUTOFF: " + reason + " - load disabled !!!");
      safety_latched = true;
    }
    safety_reason = reason;
  } else {
    safety_active = false;
    safety_latched = false;
    safety_reason = "";
  }
}

// ==================== COMMS ====================
void sendToGateway() {
  String j = "{";
  j += "\"cell1_temp\":"  + String(bat.cell_temp[0], 2) + ",";
  j += "\"cell2_temp\":"  + String(bat.cell_temp[1], 2) + ",";
  j += "\"cell3_temp\":"  + String(bat.cell_temp[2], 2) + ",";
  j += "\"ambient_temp\":"     + String(bat.ambient_temp, 2) + ",";
  j += "\"ambient_humidity\":" + String(bat.ambient_humidity, 1) + ",";
  j += "\"cell1_voltage\":" + String(bat.cell_voltage[0], 3) + ",";
  j += "\"cell2_voltage\":" + String(bat.cell_voltage[1], 3) + ",";
  j += "\"cell3_voltage\":" + String(bat.cell_voltage[2], 3) + ",";
  j += "\"pack_voltage\":"  + String(bat.pack_voltage, 3) + ",";
  j += "\"current_A\":"     + String(bat.current_A, 3) + ",";
  j += "\"power_W\":"       + String(bat.power_W, 2) + ",";
  j += "\"soc_percent\":"   + String(bat.soc_percent, 1) + ",";
  j += "\"soc_source\":\""  + String(soc_source) + "\",";
  j += "\"soh_percent\":"   + String(bat.soh_percent, 1) + ",";
  j += "\"soh_source\":\""  + String(soh_source) + "\",";
  j += "\"r0_mohm\":"       + String(r0_valid ? r0_mohm : 0.0f, 1) + ",";
  j += "\"capacity_Ah\":"   + String(measured_capacity_mAh / 1000.0f, 3) + ",";
  j += "\"cycle_discharge_Ah\":" + String(cycle_discharge_mAh / 1000.0f, 3) + ",";
  j += "\"piezo1_raw\":"    + String((int)bat.piezo_raw[0]) + ",";
  j += "\"piezo2_raw\":"    + String((int)bat.piezo_raw[1]) + ",";
  j += "\"gas_level\":"     + String(bat.gas_level) + ",";
  j += "\"cell_imbalance\":" + String(bat.cell_imbalance, 3) + ",";
  j += "\"temp_gradient\":"  + String(bat.temp_gradient, 2) + ",";
  j += "\"dT_dt\":"          + String(bat.dT_dt, 3) + ",";
  j += "\"cycles\":"         + String(bat.cycles_completed) + ",";
  j += "\"target_current\":" + String(target_current_A, 2) + ",";
  j += "\"pwm_duty\":"       + String(pwm_duty) + ",";
  j += "\"bms_tripped\":"    + String(bms_tripped ? 1 : 0) + ",";
  j += "\"safety_active\":"  + String(safety_active ? 1 : 0) + ",";
  j += "\"safety_reason\":\"" + safety_reason + "\",";
  j += "\"fault_ov\":"  + String(bat.fault_overvoltage ? 1 : 0) + ",";
  j += "\"fault_uv\":"  + String(bat.fault_undervoltage ? 1 : 0) + ",";
  j += "\"fault_ot\":"  + String(bat.fault_overtemp ? 1 : 0) + ",";
  j += "\"fault_gas\":" + String(bat.fault_gas ? 1 : 0);
  j += "}";

  Serial2.println(j);
}

void handleCommand(String cmd) {
  if (cmd.startsWith("LOAD:")) {
    float amps = constrain(cmd.substring(5).toFloat(), 0.0f, 3.0f);
    if (safety_active && amps > 0.0f) {
      Serial.println("LOAD refused - safety cutoff active: " + safety_reason);
      Serial2.println("{\"event\":\"LOAD_REFUSED\",\"reason\":\"" + safety_reason + "\"}");
      return;
    }
    if (fabs(amps - target_current_A) >= 0.3f) startR0Measurement();
    target_current_A = amps;
    bms_tripped = false;
    zero_current_count = 0;
    Serial.printf("Target current -> %.2f A\n", target_current_A);
  }
  else if (cmd.startsWith("ZERO")) {        // only at genuinely full charge
    coulomb_mAh = 0;
    soc_synced = true;
    soc_source = "ocv";
    Serial.println(F("Coulomb counter reset to 100% SOC"));
  }
  else if (cmd.startsWith("SYNC")) {        // re-sync SOC from voltage now
    if (!syncSocToOcv()) Serial.println(F("SYNC refused - voltage implausible"));
  }
  else if (cmd.startsWith("CYCLE")) {
    markCycleComplete();
  }
  else if (cmd.startsWith("CAL")) {
    target_current_A = 0;
    calibrateCurrentZero();
  }
}

void printState() {
  Serial.println(F("------------------------------"));
  Serial.printf("T: %.1f %.1f %.1f  amb %.1fC %.0f%%\n",
                bat.cell_temp[0], bat.cell_temp[1], bat.cell_temp[2],
                bat.ambient_temp, bat.ambient_humidity);
  Serial.printf("V: %.3f %.3f %.3f  pack %.3fV %s\n",
                bat.cell_voltage[0], bat.cell_voltage[1], bat.cell_voltage[2],
                bat.pack_voltage,
                packVoltagePlausible() ? "" : "[IMPLAUSIBLE - check wiring]");
  Serial.printf("I: %.3fA (target %.2fA, pwm %d)%s  P: %.2fW\n",
                bat.current_A, target_current_A, pwm_duty,
                bms_tripped ? " [BMS TRIP]" : "", bat.power_W);
  Serial.printf("SOC %.1f%% (%s)  SOH %.1f%% (%s)  R0 %.1f mOhm  cycles %d\n",
                bat.soc_percent, soc_source, bat.soh_percent, soh_source,
                r0_valid ? r0_mohm : 0.0f, bat.cycles_completed);
  Serial.printf("Piezo %d/%d  Gas %d  dT/dt %.3f\n",
                (int)bat.piezo_raw[0], (int)bat.piezo_raw[1],
                bat.gas_level, bat.dT_dt);
  if (safety_active) Serial.println("SAFETY ACTIVE: " + safety_reason);
  if (bat.fault_overvoltage || bat.fault_undervoltage ||
      bat.fault_overtemp || bat.fault_gas) {
    Serial.print(F("FAULTS:"));
    if (bat.fault_overvoltage)  Serial.print(F(" OV"));
    if (bat.fault_undervoltage) Serial.print(F(" UV"));
    if (bat.fault_overtemp)     Serial.print(F(" OT"));
    if (bat.fault_gas)          Serial.print(F(" GAS"));
    Serial.println();
  }
}

// ==================== SETUP ====================
void setup() {
  Serial.begin(115200);
  delay(500);
  Serial.println(F("\n===== ESP32 #1 : SENSOR NODE (rev 4.1) ====="));

  Serial2.begin(UART_BAUD, SERIAL_8N1, UART_RX_PIN, UART_TX_PIN);

  analogReadResolution(12);
  analogSetAttenuation(ADC_11db);

  tempSensors.begin();
  tempSensors.setResolution(12);
  Serial.printf("DS18B20 found: %d\n", tempSensors.getDeviceCount());

  dht.begin();

#if ESP_ARDUINO_VERSION_MAJOR >= 3
  ledcAttach(MOSFET_PWM_PIN, PWM_FREQ, PWM_RES_BITS);
#else
  ledcSetup(PWM_CHANNEL, PWM_FREQ, PWM_RES_BITS);
  ledcAttachPin(MOSFET_PWM_PIN, PWM_CHANNEL);
#endif
  ledcWrite(PWM_CHANNEL, 0);

  Serial.println(F("Calibrating ACS712 - keep the load DISCONNECTED..."));
  delay(1500);
  calibrateCurrentZero();
  Serial.printf("ACS712 zero = %.4f V\n", acs712_zero_volts);

  // First attempt at SOC from voltage. If the pack isn't connected yet,
  // manageSocSync() keeps trying until a plausible voltage appears.
  readCellVoltages();
  if (!syncSocToOcv()) {
    Serial.println(F("Pack voltage not plausible yet - will sync when it is"));
  }

  Serial.println(F("===== READY =====\n"));
  last_temp_time = millis();
}

// ==================== LOOP ====================
void loop() {
  unsigned long now = millis();

  // Fast loop - load regulation, R0 capture, safety
  if (now - last_control_ms >= CONTROL_INTERVAL_MS) {
    last_control_ms = now;
    readCurrent();
    checkR0Measurement();
    runCurrentControl();
    checkSafetyCutoff();
  }

  // Slow loop - full sweep, SOC/SOH, publish
  if (now - last_sensor_ms >= SENSOR_INTERVAL_MS) {
    last_sensor_ms = now;

    readTemperatures();
    readAmbient();
    readCellVoltages();
    readPiezo();
    readGas();

    updateCoulombCount();
    manageSocSync();
    calculateSOC();
    calculateSOH();
    calculateDerived();
    checkFaults();

    sendToGateway();
    printState();
  }

  while (Serial2.available()) {
    String line = Serial2.readStringUntil('\n');
    line.trim();
    if (line.length()) handleCommand(line);
  }
}
