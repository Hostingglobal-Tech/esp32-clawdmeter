#include <Arduino.h>
#include <ArduinoJson.h>
#include <BLE2902.h>
#include <BLEDevice.h>
#include <BLEServer.h>
#include <BLEUtils.h>
#include <U8g2lib.h>
#include <Wire.h>

#ifndef CLAWDMETER_LED_PIN
#define CLAWDMETER_LED_PIN 2
#endif

#ifndef CLAWDMETER_LED_ACTIVE_LOW
#define CLAWDMETER_LED_ACTIVE_LOW 0
#endif

#ifndef CLAWDMETER_OLED_SCL
#define CLAWDMETER_OLED_SCL 22
#endif

#ifndef CLAWDMETER_OLED_SDA
#define CLAWDMETER_OLED_SDA 21
#endif

#define DEVICE_NAME "Claude Controller"
#define FIRMWARE_NAME "Clawdmeter generic OLED firmware"
#define FIRMWARE_VERSION "0.3.0"
#define SERVICE_UUID "4c41555a-4465-7669-6365-000000000001"
#define RX_CHAR_UUID "4c41555a-4465-7669-6365-000000000002"
#define TX_CHAR_UUID "4c41555a-4465-7669-6365-000000000003"
#define REQ_CHAR_UUID "4c41555a-4465-7669-6365-000000000004"
#define DISPLAY_ROTATE_MS 7000

struct UsageData {
    float session_pct = 0.0f;
    int session_reset_mins = -1;
    float weekly_pct = 0.0f;
    int weekly_reset_mins = -1;
    uint32_t session_tokens = 0;
    uint32_t weekly_tokens = 0;
    char status[16] = "unknown";
    bool ok = false;
    bool valid = false;
};

static BLEServer *server = nullptr;
static BLECharacteristic *tx_char = nullptr;
static BLECharacteristic *req_char = nullptr;
static UsageData claude_usage;
static UsageData codex_usage;
static volatile bool connected = false;
static volatile bool data_seen = false;
static uint32_t last_blink = 0;
static uint32_t last_display = 0;
static uint32_t last_rotation = 0;
static bool led_on = false;
static bool show_codex = false;
static U8G2_SH1106_128X64_NONAME_F_HW_I2C display(
    U8G2_R0, U8X8_PIN_NONE, CLAWDMETER_OLED_SCL, CLAWDMETER_OLED_SDA);

static void set_led(bool on) {
    led_on = on;
#if CLAWDMETER_LED_ACTIVE_LOW
    digitalWrite(CLAWDMETER_LED_PIN, on ? LOW : HIGH);
#else
    digitalWrite(CLAWDMETER_LED_PIN, on ? HIGH : LOW);
#endif
}

static int pct_i(float pct) {
    if (pct < 0.0f) return 0;
    if (pct > 100.0f) return 100;
    return (int)(pct + 0.5f);
}

static const char *ble_state_label() {
    if (connected) return data_seen ? "CONN" : "WAIT";
    return "ADV";
}

static const UsageData *active_usage() {
    return show_codex ? &codex_usage : &claude_usage;
}

static const char *active_name() {
    return show_codex ? "Codex" : "Claude";
}

static const char *usage_state_label(const UsageData *u) {
    if (!u->valid) return "NO DATA";
    if (strcmp(u->status, "limited") == 0) return "LIMITED";
    if (strcmp(u->status, "est") == 0) return "EST";
    if (u->session_pct >= 75.0f || u->weekly_pct >= 85.0f) return "HIGH";
    if (u->session_pct >= 50.0f || u->weekly_pct >= 65.0f) return "WATCH";
    return u->ok ? "OK" : "CHECK";
}

static void draw_bar(uint8_t x, uint8_t y, uint8_t w, uint8_t h, int pct) {
    display.drawFrame(x, y, w, h);
    int fill_w = ((int)(w - 2) * pct) / 100;
    if (fill_w > 0) display.drawBox(x + 1, y + 1, fill_w, h - 2);
}

static void format_tokens(uint32_t tokens, char *out, size_t out_len) {
    if (tokens >= 10000000UL) {
        snprintf(out, out_len, "%luM", (unsigned long)((tokens + 500000UL) / 1000000UL));
    } else if (tokens >= 1000000UL) {
        snprintf(out, out_len, "%lu.%luM",
                 (unsigned long)(tokens / 1000000UL),
                 (unsigned long)((tokens % 1000000UL) / 100000UL));
    } else if (tokens >= 1000UL) {
        snprintf(out, out_len, "%luk", (unsigned long)((tokens + 500UL) / 1000UL));
    } else {
        snprintf(out, out_len, "%lu", (unsigned long)tokens);
    }
}

static void draw_display(bool force = false) {
    uint32_t now = millis();
    if (!force && now - last_display < 500) return;
    last_display = now;

    const UsageData *usage = active_usage();
    int session = usage->valid ? pct_i(usage->session_pct) : 0;
    int weekly = usage->valid ? pct_i(usage->weekly_pct) : 0;

    char line[32];
    display.clearBuffer();

    display.setFont(u8g2_font_6x10_tf);
    snprintf(line, sizeof(line), "%s %s", active_name(), ble_state_label());
    display.drawStr(0, 8, line);

    display.drawHLine(0, 11, 128);

    if (usage->valid) {
        snprintf(line, sizeof(line), "%-7s %3d%%", show_codex ? "5h" : "Session", session);
    } else {
        snprintf(line, sizeof(line), "%-7s  --%%", show_codex ? "5h" : "Session");
    }
    display.drawStr(0, 23, line);
    draw_bar(0, 26, 128, 7, session);

    if (usage->valid) {
        snprintf(line, sizeof(line), "%-7s %3d%%", show_codex ? "7d" : "Weekly", weekly);
    } else {
        snprintf(line, sizeof(line), "%-7s  --%%", show_codex ? "7d" : "Weekly");
    }
    display.drawStr(0, 43, line);
    draw_bar(0, 46, 128, 7, weekly);

    if (show_codex && usage->valid && usage->session_tokens > 0) {
        char tokens[8];
        format_tokens(usage->session_tokens, tokens, sizeof(tokens));
        snprintf(line, sizeof(line), "%s 5h %s tok", usage_state_label(usage), tokens);
    } else if (usage->valid && usage->session_reset_mins >= 0) {
        snprintf(line, sizeof(line), "%s reset %dm", usage_state_label(usage), usage->session_reset_mins);
    } else {
        snprintf(line, sizeof(line), "%s %s", usage_state_label(usage), DEVICE_NAME);
    }
    line[21] = '\0';
    display.drawStr(0, 63, line);

    display.sendBuffer();
}

static bool parse_usage(JsonVariantConst obj, UsageData *out) {
    if (obj.isNull()) return false;

    out->session_pct = obj["s"] | 0.0f;
    out->session_reset_mins = obj["sr"] | -1;
    out->weekly_pct = obj["w"] | 0.0f;
    out->weekly_reset_mins = obj["wr"] | -1;
    out->session_tokens = obj["t5"] | 0UL;
    out->weekly_tokens = obj["t7"] | 0UL;
    strlcpy(out->status, obj["st"] | "unknown", sizeof(out->status));
    out->ok = obj["ok"] | false;
    out->valid = true;
    return true;
}

static bool parse_json(const char *json) {
    JsonDocument doc;
    DeserializationError err = deserializeJson(doc, json);
    if (err) {
        Serial.printf("JSON parse error: %s\n", err.c_str());
        return false;
    }

    bool parsed = false;
    JsonVariantConst claude = doc["claude"];
    JsonVariantConst codex = doc["codex"];

    if (!claude.isNull() || !codex.isNull()) {
        if (parse_usage(claude, &claude_usage)) parsed = true;
        if (parse_usage(codex, &codex_usage)) parsed = true;
        return parsed;
    }

    // Backward compatibility with the original flat payload.
    return parse_usage(doc.as<JsonVariantConst>(), &claude_usage);
}

static void send_ack(bool ok) {
    if (!connected || tx_char == nullptr) return;
    tx_char->setValue(ok ? "{\"ack\":true}" : "{\"err\":true}");
    tx_char->notify();
}

static void request_refresh() {
    if (!connected || req_char == nullptr || data_seen) return;
    uint8_t v = 0x01;
    req_char->setValue(&v, 1);
    req_char->notify();
    Serial.println("BLE: refresh requested");
}

static uint32_t blink_interval_ms() {
    if (!connected) return 1000;
    if (!claude_usage.valid && !codex_usage.valid) return 300;
    if ((claude_usage.valid && strcmp(claude_usage.status, "limited") == 0) ||
        (codex_usage.valid && strcmp(codex_usage.status, "limited") == 0)) {
        return 100;
    }

    float pct = 0.0f;
    if (claude_usage.valid && claude_usage.session_pct > pct) pct = claude_usage.session_pct;
    if (codex_usage.valid && codex_usage.session_pct > pct) pct = codex_usage.session_pct;

    if (pct >= 75.0f) return 150;
    if (pct >= 50.0f) return 300;
    if (pct >= 25.0f) return 600;
    return 1200;
}

static void update_led() {
    uint32_t now = millis();
    uint32_t interval = blink_interval_ms();
    if (now - last_blink >= interval) {
        last_blink = now;
        set_led(!led_on);
    }
}

class ServerCallbacks : public BLEServerCallbacks {
    void onConnect(BLEServer *s) override {
        connected = true;
        Serial.printf("BLE: connected, peers=%u\n", (unsigned)s->getConnectedCount());
        set_led(true);
        request_refresh();
    }

    void onDisconnect(BLEServer *s) override {
        connected = false;
        Serial.println("BLE: disconnected, restarting advertising");
        set_led(false);
        BLEDevice::startAdvertising();
    }
};

class RxCallbacks : public BLECharacteristicCallbacks {
    void onWrite(BLECharacteristic *chr) override {
        auto value = chr->getValue();
        if (value.length() == 0) {
            send_ack(false);
            return;
        }

        if (parse_json(value.c_str())) {
            data_seen = true;
            Serial.printf(
                "Claude: %.1f%%/%dmin %.1f%%/%dmin %s | Codex: %.1f%% %.1f%% %s\n",
                claude_usage.session_pct,
                claude_usage.session_reset_mins,
                claude_usage.weekly_pct,
                claude_usage.weekly_reset_mins,
                claude_usage.status,
                codex_usage.session_pct,
                codex_usage.weekly_pct,
                codex_usage.status);
            send_ack(true);
            draw_display(true);
        } else {
            send_ack(false);
        }
    }
};

static void init_ble() {
    BLEDevice::init(DEVICE_NAME);
    BLEDevice::setMTU(185);

    server = BLEDevice::createServer();
    static ServerCallbacks server_callbacks;
    server->setCallbacks(&server_callbacks);

    BLEService *service = server->createService(SERVICE_UUID);

    BLECharacteristic *rx_char = service->createCharacteristic(
        RX_CHAR_UUID,
        BLECharacteristic::PROPERTY_WRITE | BLECharacteristic::PROPERTY_WRITE_NR);
    static RxCallbacks rx_callbacks;
    rx_char->setCallbacks(&rx_callbacks);

    tx_char = service->createCharacteristic(
        TX_CHAR_UUID,
        BLECharacteristic::PROPERTY_READ | BLECharacteristic::PROPERTY_NOTIFY);
    tx_char->addDescriptor(new BLE2902());

    req_char = service->createCharacteristic(
        REQ_CHAR_UUID,
        BLECharacteristic::PROPERTY_NOTIFY);
    req_char->addDescriptor(new BLE2902());

    service->start();

    BLEAdvertising *advertising = BLEDevice::getAdvertising();
    advertising->addServiceUUID(SERVICE_UUID);
    advertising->setScanResponse(true);
    advertising->setMinPreferred(0x06);
    advertising->setMinPreferred(0x12);
    BLEDevice::startAdvertising();

    Serial.printf("BLE: advertising as %s\n", DEVICE_NAME);
}

void setup() {
    Serial.begin(115200);
    delay(300);

    pinMode(CLAWDMETER_LED_PIN, OUTPUT);
    set_led(false);

    Serial.println();
    Serial.printf("%s v%s\n", FIRMWARE_NAME, FIRMWARE_VERSION);
    Serial.printf("LED pin: GPIO%d\n", CLAWDMETER_LED_PIN);
    Serial.printf("OLED: SH1106 I2C SDA=GPIO%d SCL=GPIO%d\n",
                  CLAWDMETER_OLED_SDA, CLAWDMETER_OLED_SCL);

    Wire.begin(CLAWDMETER_OLED_SDA, CLAWDMETER_OLED_SCL);
    display.begin();
    display.setPowerSave(0);
    display.setContrast(180);
    draw_display(true);

    init_ble();
    draw_display(true);
}

void loop() {
    uint32_t now = millis();
    if (now - last_rotation >= DISPLAY_ROTATE_MS) {
        last_rotation = now;
        show_codex = !show_codex;
        draw_display(true);
    }
    update_led();
    draw_display();
    delay(5);
}
