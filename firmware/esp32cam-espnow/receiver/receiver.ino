// ESP-NOW image receiver -> USB serial to the Raspberry Pi.
//
// Original design and code: Lucian H (lucianbuilds), September 2026. Any
// ESP32 works; Lucian used an ESP32-S3 board for its external IPEX antenna,
// which is what decides the range. Plugged into the Pi (or any computer)
// over USB; pi/espnow_bridge.py on the other end rebuilds the JPEGs.
//
// Changes from the original, reviewed into the repo (see README):
//   - keys and the transmitter allow-list live in config.h (gitignored)
//   - only listed transmitters are peers, and they are ENCRYPTED peers:
//     a packet from any other MAC is dropped before it is parsed, and a
//     listed board without the right key cannot be decrypted either
//   - the frame-start packet's image length + CRC32 are forwarded to the
//     Pi in every chunk header, so the Pi's integrity check is real
//
// Arduino-ESP32 core 3.x.

#include <Arduino.h>
#include <WiFi.h>
#include <esp_now.h>
#include <esp_wifi.h>
#include <esp_mac.h>
#include "config.h"

#define MAX_PAYLOAD 220       // must match the transmitter
#define RX_QUEUE_SIZE 20

#define PKT_START 0x00
#define PKT_DATA  0x01
#define PKT_ACK   0x02
#define SEQ_START 0xFFFF

static const uint8_t ALLOWED[][6] = ALLOWED_TRANSMITTERS;
static const int ALLOWED_COUNT = sizeof(ALLOWED) / 6;

// Serial header the Pi expects: struct "<4sIHHHII" = 22 bytes.
struct __attribute__((packed)) SerialPacketHeader {
    char     magic[4];
    uint32_t frame_id;
    uint16_t seq;
    uint16_t total;
    uint16_t payload_len;
    uint32_t image_len;
    uint32_t image_crc;
};
static_assert(sizeof(SerialPacketHeader) == 22, "SerialPacketHeader must be 22 bytes");

struct __attribute__((packed)) AckPacket {
    uint8_t  type;
    uint32_t frame_id;
    uint16_t seq;
};
static_assert(sizeof(AckPacket) == 7, "AckPacket must be 7 bytes");

struct ReceivedPacket {
    uint8_t  source_mac[6];
    uint16_t len;
    uint8_t  data[250];
};

QueueHandle_t rxQueue = nullptr;

// What the current frame claims about itself, from its start packet.
static uint32_t curFrame = 0, curLen = 0, curCrc = 0;

static bool isAllowed(const uint8_t *mac) {
    for (int i = 0; i < ALLOWED_COUNT; i++) {
        if (memcmp(ALLOWED[i], mac, 6) == 0) return true;
    }
    return false;
}

static void printMac(const char *label, const uint8_t *mac) {
    Serial.print(label);
    for (int i = 0; i < 6; i++) {
        if (mac[i] < 16) Serial.print("0");
        Serial.print(mac[i], HEX);
        if (i < 5) Serial.print(":");
    }
    Serial.println();
}

// Keep the callback short: allow-list check, copy into the queue, leave.
void onReceive(const esp_now_recv_info_t *info, const uint8_t *data, int len) {
    if (info == nullptr || data == nullptr || len <= 0 || len > 250) return;
    if (!isAllowed(info->src_addr)) return;   // strangers never reach the parser
    ReceivedPacket packet;
    memcpy(packet.source_mac, info->src_addr, 6);
    packet.len = len;
    memcpy(packet.data, data, len);
    xQueueSend(rxQueue, &packet, 0);         // never block inside the callback
}

void sendAck(const uint8_t *destination, uint32_t frameId, uint16_t seq) {
    AckPacket ack;
    ack.type = PKT_ACK;
    ack.frame_id = frameId;
    ack.seq = seq;
    esp_err_t result = esp_now_send(destination, (uint8_t *)&ack, sizeof(ack));
    if (result != ESP_OK) {
        Serial.print("ACK send error: ");
        Serial.println(result);
    }
}

void sendToPi(uint32_t frameId, uint16_t seq, uint16_t total,
              const uint8_t *payload, uint16_t payloadLen,
              uint32_t imageLen, uint32_t imageCRC) {
    SerialPacketHeader header;
    memcpy(header.magic, "SPKT", 4);
    header.frame_id = frameId;
    header.seq = seq;
    header.total = total;
    header.payload_len = payloadLen;
    header.image_len = imageLen;
    header.image_crc = imageCRC;
    Serial.write((const uint8_t *)&header, sizeof(header));
    if (payloadLen > 0) Serial.write(payload, payloadLen);
    Serial.flush();
}

void processPacket(const ReceivedPacket &packet) {
    const uint8_t *p = packet.data;
    if (packet.len < 1) return;

    if (p[0] == PKT_START) {
        if (packet.len != 15) return;
        curFrame = (uint32_t)p[1] | ((uint32_t)p[2] << 8) | ((uint32_t)p[3] << 16) | ((uint32_t)p[4] << 24);
        curLen   = (uint32_t)p[7] | ((uint32_t)p[8] << 8) | ((uint32_t)p[9] << 16) | ((uint32_t)p[10] << 24);
        curCrc   = (uint32_t)p[11] | ((uint32_t)p[12] << 8) | ((uint32_t)p[13] << 16) | ((uint32_t)p[14] << 24);
        sendAck(packet.source_mac, curFrame, SEQ_START);
        return;
    }
    if (p[0] != PKT_DATA || packet.len < 11) return;

    uint32_t frameId = (uint32_t)p[1] | ((uint32_t)p[2] << 8) | ((uint32_t)p[3] << 16) | ((uint32_t)p[4] << 24);
    uint16_t seq = (uint16_t)p[5] | ((uint16_t)p[6] << 8);
    uint16_t total = (uint16_t)p[7] | ((uint16_t)p[8] << 8);
    uint16_t payloadLen = (uint16_t)p[9] | ((uint16_t)p[10] << 8);

    if (total == 0 || seq >= total || payloadLen > MAX_PAYLOAD ||
        packet.len != 11 + payloadLen) {
        Serial.println("Invalid image packet");
        return;
    }
    // Length/CRC only if the start packet for THIS frame was seen; otherwise
    // zeros, which the Pi treats as "unknown" rather than "matches".
    uint32_t imageLen = frameId == curFrame ? curLen : 0;
    uint32_t imageCrc = frameId == curFrame ? curCrc : 0;

    sendToPi(frameId, seq, total, p + 11, payloadLen, imageLen, imageCrc);
    sendAck(packet.source_mac, frameId, seq);   // ACK only after the Pi has it
}

void setup() {
    Serial.begin(SERIAL_BAUD);
    delay(1000);
    Serial.println();
    Serial.println("==============================");
    Serial.println("ESP-NOW RECEIVER");
    Serial.println("==============================");

    WiFi.mode(WIFI_STA);
    delay(100);
    esp_err_t result = esp_wifi_set_channel(ESPNOW_CHANNEL, WIFI_SECOND_CHAN_NONE);
    if (result != ESP_OK) {
        Serial.print("ERROR setting WiFi channel: ");
        Serial.println(result);
    }

    uint8_t mac[6];
    if (esp_read_mac(mac, ESP_MAC_WIFI_STA) == ESP_OK) {
        printMac("Receiver MAC: ", mac);        // goes in the transmitter's config.h
    }
    Serial.print("ESP-NOW channel: "); Serial.println(ESPNOW_CHANNEL);
    Serial.print("Serial baud: ");     Serial.println(SERIAL_BAUD);

    rxQueue = xQueueCreate(RX_QUEUE_SIZE, sizeof(ReceivedPacket));
    if (rxQueue == nullptr) {
        Serial.println("ERROR: Failed to create RX queue");
        while (true) delay(1000);
    }

    result = esp_now_init();
    if (result != ESP_OK) {
        Serial.print("ERROR: ESP-NOW init failed: ");
        Serial.println(result);
        while (true) delay(1000);
    }
    static_assert(sizeof(ESPNOW_PMK) == 17, "ESPNOW_PMK must be exactly 16 characters");
    static_assert(sizeof(ESPNOW_LMK) == 17, "ESPNOW_LMK must be exactly 16 characters");
    result = esp_now_set_pmk((const uint8_t *)ESPNOW_PMK);
    if (result != ESP_OK) {
        Serial.print("ERROR: esp_now_set_pmk failed: ");
        Serial.println(result);
        while (true) delay(1000);
    }

    // Every listed transmitter is an encrypted peer from the start. Nothing
    // is added at runtime: an unlisted board is dropped in onReceive.
    int added = 0;
    for (int i = 0; i < ALLOWED_COUNT; i++) {
        esp_now_peer_info_t peer = {};
        memcpy(peer.peer_addr, ALLOWED[i], 6);
        peer.channel = ESPNOW_CHANNEL;
        peer.encrypt = true;
        memcpy(peer.lmk, ESPNOW_LMK, 16);
        result = esp_now_add_peer(&peer);
        if (result == ESP_OK || result == ESP_ERR_ESPNOW_EXIST) {
            printMac("Allowed transmitter: ", ALLOWED[i]);
            added++;
        } else {
            Serial.print("Failed to add peer: ");
            Serial.println(result);
        }
    }
    if (added == 0) {
        Serial.println("WARNING: no transmitters in ALLOWED_TRANSMITTERS - nothing will be accepted");
    }

    esp_now_register_recv_cb(onReceive);
    Serial.println("ESP-NOW initialized, link encrypted (PMK+LMK)");
    Serial.println("Waiting for transmitter...");
    Serial.println();
}

void loop() {
    ReceivedPacket packet;
    if (xQueueReceive(rxQueue, &packet, pdMS_TO_TICKS(100)) == pdTRUE) {
        processPacket(packet);
    }
    delay(1);
}
