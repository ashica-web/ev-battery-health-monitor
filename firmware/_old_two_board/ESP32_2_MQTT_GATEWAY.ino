/*
=========================================================
ESP32 #2 - MQTT GATEWAY
=========================================================
REVISION 2 - corrected UART pins, non-blocking reconnect, amp-based load

CHANGES FROM REV 1:
  * UART moved from GPIO1/3 to GPIO16/17
  * Reconnect logic no longer blocks the loop (REV 1 could stall for
    10+ seconds and lose all incoming UART data)
  * Load command is now in AMPS, not raw PWM - the dashboard asks for a
    current and ESP32 #1's control loop delivers it
  * Passes the JSON through unchanged instead of re-serialising it,
    so new fields on ESP32 #1 appear automatically with no edit here
  * Retained flag set on state topic so a dashboard that connects late
    immediately sees the last known state
  * Last Will message so subscribers know if the gateway drops off

Board: ESP32 Dev Module
IDE: Arduino IDE 2.x
Libraries: PubSubClient, ArduinoJson
*/

#include <WiFi.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>

// ==================== EDIT THESE ====================
const char* WIFI_SSID     = "YOUR_WIFI_SSID";
const char* WIFI_PASSWORD = "YOUR_WIFI_PASSWORD";

const char* MQTT_HOST = "192.168.1.100";   // IP of your Mosquitto machine
const int   MQTT_PORT = 1883;
const char* MQTT_USER = "";                // leave "" if broker has no auth
const char* MQTT_PASS = "";
const char* MQTT_CLIENT_ID = "esp32_battery_gateway";
// ====================================================

// ==================== PINS ====================
#define UART_RX_PIN   16    // <- from ESP32 #1 GPIO17 (TX)
#define UART_TX_PIN   17    // -> to   ESP32 #1 GPIO16 (RX)
#define UART_BAUD 115200

// ==================== TOPICS ====================
#define T_STATE     "battery/state"        // full JSON, retained
#define T_STATUS    "battery/status"       // online / offline
#define T_EVENT     "battery/event"        // safety cutoff etc
#define T_CMD_LOAD  "battery/cmd/load"     // IN: target current in amps
#define T_CMD_SYS   "battery/cmd/sys"      // IN: ZERO / CYCLE / CAL

// Flat topics - handy for Node-RED, MQTT Explorer, Grafana
#define T_SOC       "battery/soc"
#define T_SOH       "battery/soh"
#define T_VOLTAGE   "battery/voltage"
#define T_CURRENT   "battery/current"
#define T_POWER     "battery/power"
#define T_TEMP1     "battery/temp/cell1"
#define T_TEMP2     "battery/temp/cell2"
#define T_TEMP3     "battery/temp/cell3"

// ==================== OBJECTS ====================
WiFiClient   net;
PubSubClient mqtt(net);

// Non-blocking reconnect bookkeeping
unsigned long last_wifi_try = 0;
unsigned long last_mqtt_try = 0;
#define RETRY_INTERVAL_MS 5000

// Stats
unsigned long msgs_received = 0;
unsigned long msgs_published = 0;
unsigned long last_rx_ms = 0;

// ==================== SETUP ====================
void setup() {
  Serial.begin(115200);
  delay(500);
  Serial.println(F("\n===== ESP32 #2 : MQTT GATEWAY ====="));

  Serial2.begin(UART_BAUD, SERIAL_8N1, UART_RX_PIN, UART_TX_PIN);
  Serial.println(F("UART2 up on GPIO16(RX)/GPIO17(TX)"));

  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  startWiFi();

  mqtt.setServer(MQTT_HOST, MQTT_PORT);
  mqtt.setCallback(onMqttMessage);
  mqtt.setBufferSize(1024);     // default 256 is too small for our JSON

  Serial.println(F("===== READY =====\n"));
}

// ==================== LOOP ====================
void loop() {
  maintainWiFi();
  maintainMQTT();
  mqtt.loop();

  // Relay anything arriving from ESP32 #1
  while (Serial2.available()) {
    String line = Serial2.readStringUntil('\n');
    line.trim();
    if (line.length() > 2) handleFromSensorNode(line);
  }

  // Warn if the sensor node has gone quiet
  if (last_rx_ms && millis() - last_rx_ms > 5000) {
    Serial.println(F("WARNING: no data from ESP32 #1 for 5s - check UART"));
    last_rx_ms = millis();
  }
}

// ==================== WIFI ====================
void startWiFi() {
  Serial.print(F("WiFi connecting to "));
  Serial.println(WIFI_SSID);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  last_wifi_try = millis();
}

void maintainWiFi() {
  static bool was_connected = false;

  if (WiFi.status() == WL_CONNECTED) {
    if (!was_connected) {
      Serial.print(F("WiFi connected, IP "));
      Serial.println(WiFi.localIP());
      was_connected = true;
    }
    return;
  }

  was_connected = false;
  if (millis() - last_wifi_try > RETRY_INTERVAL_MS) {
    Serial.println(F("WiFi retry..."));
    WiFi.disconnect();
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
    last_wifi_try = millis();
  }
}

// ==================== MQTT ====================
void maintainMQTT() {
  if (WiFi.status() != WL_CONNECTED) return;
  if (mqtt.connected()) return;

  if (millis() - last_mqtt_try < RETRY_INTERVAL_MS) return;
  last_mqtt_try = millis();

  Serial.print(F("MQTT connecting to "));
  Serial.print(MQTT_HOST);
  Serial.print(':');
  Serial.println(MQTT_PORT);

  bool ok;
  if (strlen(MQTT_USER) > 0) {
    // Last Will: broker publishes "offline" if we drop unexpectedly
    ok = mqtt.connect(MQTT_CLIENT_ID, MQTT_USER, MQTT_PASS,
                      T_STATUS, 0, true, "offline");
  } else {
    ok = mqtt.connect(MQTT_CLIENT_ID, T_STATUS, 0, true, "offline");
  }

  if (ok) {
    Serial.println(F("MQTT connected"));
    mqtt.publish(T_STATUS, "online", true);
    mqtt.subscribe(T_CMD_LOAD);
    mqtt.subscribe(T_CMD_SYS);
    Serial.println(F("Subscribed to command topics"));
  } else {
    Serial.print(F("MQTT failed, rc="));
    Serial.println(mqtt.state());
  }
}

void onMqttMessage(char* topic, byte* payload, unsigned int len) {
  String t = String(topic);
  String p;
  p.reserve(len);
  for (unsigned int i = 0; i < len; i++) p += (char)payload[i];
  p.trim();

  Serial.print(F("CMD "));
  Serial.print(t);
  Serial.print(F(" = "));
  Serial.println(p);

  if (t == T_CMD_LOAD) {
    // Dashboard sends amps, e.g. "1.5"
    float amps = p.toFloat();
    amps = constrain(amps, 0.0f, 3.0f);
    Serial2.print("LOAD:");
    Serial2.println(amps, 2);
  }
  else if (t == T_CMD_SYS) {
    // ZERO | CYCLE | CAL | SYNC - forwarded verbatim
    p.toUpperCase();
    if (p == "ZERO" || p == "CYCLE" || p == "CAL" || p == "SYNC") {
      Serial2.println(p);
    }
  }
}

// ==================== RELAY ====================
void handleFromSensorNode(String line) {
  msgs_received++;
  last_rx_ms = millis();

  if (!mqtt.connected()) return;

  // Events (safety cutoff) go to their own topic
  if (line.indexOf("\"event\"") >= 0) {
    mqtt.publish(T_EVENT, line.c_str());
    Serial.println(F("EVENT relayed"));
    return;
  }

  // Full state passes through unchanged - any new field ESP32 #1 adds
  // shows up on the dashboard with no change needed here.
  mqtt.publish(T_STATE, line.c_str(), true);

  // Also break out the headline numbers onto flat topics
  publishFlatTopics(line);

  msgs_published++;
  if (msgs_published % 10 == 0) {
    Serial.print(F("Relayed "));
    Serial.print(msgs_published);
    Serial.println(F(" messages"));
  }
}

void publishFlatTopics(const String& json) {
  StaticJsonDocument<2048> doc;   // rev 4.1 messages are larger
  if (deserializeJson(doc, json)) {
    Serial.println(F("JSON parse failed - skipping flat topics"));
    return;
  }

  pubNum(T_SOC,     doc["soc_percent"]  | 0.0f, 1);
  pubNum(T_SOH,     doc["soh_percent"]  | 0.0f, 1);
  pubNum(T_VOLTAGE, doc["pack_voltage"] | 0.0f, 3);
  pubNum(T_CURRENT, doc["current_A"]    | 0.0f, 3);
  pubNum(T_POWER,   doc["power_W"]      | 0.0f, 2);
  pubNum(T_TEMP1,   doc["cell1_temp"]   | 0.0f, 2);
  pubNum(T_TEMP2,   doc["cell2_temp"]   | 0.0f, 2);
  pubNum(T_TEMP3,   doc["cell3_temp"]   | 0.0f, 2);
}

void pubNum(const char* topic, float v, int decimals) {
  char buf[16];
  dtostrf(v, 0, decimals, buf);
  mqtt.publish(topic, buf);
}
