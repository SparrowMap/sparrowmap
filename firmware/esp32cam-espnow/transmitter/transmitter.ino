// ESP32-CAM -> ESP-NOW image transmitter.
//
// Original design and code: Lucian H (lucianbuilds), September 2026 - the
// first SparrowMap camera node not built by the project's author. Takes a
// still every CAPTURE_INTERVAL_MS, splits it into ESP-NOW packets with a
// per-packet ACK and retry, and sends it to ONE receiver board that hands it
// to a Raspberry Pi over USB serial. The camera never joins a WiFi network
// and has no route to the internet: recognition, cropping and the plate
// guarantee all happen on the Pi, exactly where SparrowMap wants them.
//
// Changes from the original, reviewed into the repo (see README):
//   - keys and MACs live in config.h (gitignored), not in the sketch
//   - the ESP-NOW link is ENCRYPTED (PMK + LMK): a still of a public road
//     with readable plates in it does not cross the air in the clear, and a
//     board without the key cannot feed the node
//   - a frame-start packet carries image length + CRC32, so the Pi-side
//     integrity check is real rather than decorative
//   - the board prints its own MAC at boot for the receiver's allow-list
//
// Board: AI-Thinker ESP32-CAM (e.g. the Hosyond kit with the USB-UART
// dongle). Arduino-ESP32 core 3.x (the esp_now_recv_info_t callback).

#include <Arduino.h>
#include <WiFi.h>
#include <esp_now.h>
#include <esp_wifi.h>
#include <esp_mac.h>
#include "esp_camera.h"
#include "config.h"

// ESP-NOW v1 max packet = 250 bytes; 11 bytes are our data header.
#define MAX_PAYLOAD 220
#define ACK_TIMEOUT_MS 200
#define MAX_RETRIES 8

// Packet types on the air.
#define PKT_START 0x00   // frame_id, total, image_len, crc32   (15 bytes)
#define PKT_DATA  0x01   // frame_id, seq, total, len, payload  (11 + len)
#define PKT_ACK   0x02   // frame_id, seq                       (7 bytes)
#define SEQ_START 0xFFFF // the ACK sequence used for a start packet

// ---- AI-Thinker ESP32-CAM pins ------------------------------------------
#define PWDN_GPIO_NUM     32
#define RESET_GPIO_NUM    -1
#define XCLK_GPIO_NUM      0
#define SIOD_GPIO_NUM     26
#define SIOC_GPIO_NUM     27
#define Y9_GPIO_NUM       35
#define Y8_GPIO_NUM       34
#define Y7_GPIO_NUM       39
#define Y6_GPIO_NUM       36
#define Y5_GPIO_NUM       21
#define Y4_GPIO_NUM       19
#define Y3_GPIO_NUM       18
#define Y2_GPIO_NUM        5
#define VSYNC_GPIO_NUM    25
#define HREF_GPIO_NUM     23
#define PCLK_GPIO_NUM     22

static uint8_t RECEIVER[] = RECEIVER_MAC;

// ---- ACK state (written by the receive callback) --------------------------
volatile bool     ackReceived = false;
volatile uint32_t ackFrame = 0;
volatile uint16_t ackSeq = 0;

uint32_t frameCounter = 0;

// Standard CRC-32 (poly 0xEDB88320), identical to Python's zlib.crc32.
uint32_t crc32(const uint8_t *data, size_t length) {
    uint32_t crc = 0xFFFFFFFF;
    for (size_t i = 0; i < length; i++) {
        crc ^= data[i];
        for (uint8_t j = 0; j < 8; j++) {
            crc = (crc & 1) ? (crc >> 1) ^ 0xEDB88320 : crc >> 1;
        }
    }
    return crc ^ 0xFFFFFFFF;
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

// ---- ACKs from the receiver ------------------------------------------------
void onDataRecv(const esp_now_recv_info_t *info, const uint8_t *data, int len) {
    if (info == nullptr || data == nullptr || len != 7 || data[0] != PKT_ACK) return;
    // Only the configured receiver may ACK. Anything else on the channel is
    // noise, and an ACK from a stranger must not advance the frame.
    if (memcmp(info->src_addr, RECEIVER, 6) != 0) return;
    uint32_t frameId = (uint32_t)data[1] | ((uint32_t)data[2] << 8) |
                       ((uint32_t)data[3] << 16) | ((uint32_t)data[4] << 24);
    uint16_t seq = (uint16_t)data[5] | ((uint16_t)data[6] << 8);
    ackFrame = frameId;
    ackSeq = seq;
    ackReceived = true;
}

// Send one already-built packet until the receiver ACKs (frameId, ackSeqWanted).
bool sendReliably(const uint8_t *packet, uint16_t packetLen,
                  uint32_t frameId, uint16_t ackSeqWanted) {
    if (packetLen > 250) {
        Serial.println("ERROR: ESP-NOW packet too large");
        return false;
    }
    for (int attempt = 1; attempt <= MAX_RETRIES; attempt++) {
        ackReceived = false;
        ackFrame = 0;
        ackSeq = 0;
        esp_err_t result = esp_now_send(RECEIVER, packet, packetLen);
        if (result != ESP_OK) {
            Serial.print("esp_now_send error: ");
            Serial.println(result);
            delay(10);
            continue;
        }
        unsigned long start = millis();
        while (millis() - start < ACK_TIMEOUT_MS) {
            if (ackReceived && ackFrame == frameId && ackSeq == ackSeqWanted) {
                return true;
            }
            delay(1);
        }
        Serial.print("Retry ");
        Serial.print(ackSeqWanted == SEQ_START ? "start" : String(ackSeqWanted).c_str());
        Serial.print(" (attempt ");
        Serial.print(attempt);
        Serial.println(")");
    }
    return false;
}

// Frame-start packet: what the receiver needs to check the whole image.
//   0     type
//   1-4   frame id
//   5-6   total packets
//   7-10  image length
//   11-14 CRC32 of the whole JPEG
bool sendStart(uint32_t frameId, uint16_t total, uint32_t imageLen, uint32_t crc) {
    uint8_t p[15];
    p[0] = PKT_START;
    p[1] = frameId & 0xFF; p[2] = (frameId >> 8) & 0xFF;
    p[3] = (frameId >> 16) & 0xFF; p[4] = (frameId >> 24) & 0xFF;
    p[5] = total & 0xFF; p[6] = (total >> 8) & 0xFF;
    p[7] = imageLen & 0xFF; p[8] = (imageLen >> 8) & 0xFF;
    p[9] = (imageLen >> 16) & 0xFF; p[10] = (imageLen >> 24) & 0xFF;
    p[11] = crc & 0xFF; p[12] = (crc >> 8) & 0xFF;
    p[13] = (crc >> 16) & 0xFF; p[14] = (crc >> 24) & 0xFF;
    return sendReliably(p, sizeof(p), frameId, SEQ_START);
}

// Data packet:
//   0     type
//   1-4   frame id
//   5-6   sequence
//   7-8   total packets
//   9-10  payload length
//   11..  JPEG bytes
bool sendData(uint32_t frameId, uint16_t seq, uint16_t total,
              const uint8_t *payload, uint16_t payloadLen) {
    if (payloadLen > MAX_PAYLOAD) {
        Serial.println("ERROR: payload too large");
        return false;
    }
    uint8_t packet[11 + MAX_PAYLOAD];
    packet[0] = PKT_DATA;
    packet[1] = frameId & 0xFF; packet[2] = (frameId >> 8) & 0xFF;
    packet[3] = (frameId >> 16) & 0xFF; packet[4] = (frameId >> 24) & 0xFF;
    packet[5] = seq & 0xFF; packet[6] = (seq >> 8) & 0xFF;
    packet[7] = total & 0xFF; packet[8] = (total >> 8) & 0xFF;
    packet[9] = payloadLen & 0xFF; packet[10] = (payloadLen >> 8) & 0xFF;
    memcpy(packet + 11, payload, payloadLen);
    return sendReliably(packet, 11 + payloadLen, frameId, seq);
}

bool transmitImage(const uint8_t *image, size_t imageSize) {
    uint32_t frameId = ++frameCounter;
    uint16_t totalPackets = (imageSize + MAX_PAYLOAD - 1) / MAX_PAYLOAD;
    uint32_t imageCRC = crc32(image, imageSize);

    Serial.println();
    Serial.println("------------------------------");
    Serial.print("Frame: ");      Serial.println(frameId);
    Serial.print("JPEG size: ");  Serial.print(imageSize); Serial.println(" bytes");
    Serial.print("Packets: ");    Serial.println(totalPackets);
    Serial.print("CRC32: ");      Serial.println(imageCRC, HEX);

    if (!sendStart(frameId, totalPackets, imageSize, imageCRC)) {
        Serial.println("FAILED at frame start (receiver not answering?)");
        return false;
    }
    for (uint16_t seq = 0; seq < totalPackets; seq++) {
        size_t offset = (size_t)seq * MAX_PAYLOAD;
        size_t remaining = imageSize - offset;
        uint16_t payloadLen = remaining > MAX_PAYLOAD ? MAX_PAYLOAD : remaining;
        if (!sendData(frameId, seq, totalPackets, image + offset, payloadLen)) {
            Serial.print("FAILED at packet ");
            Serial.println(seq);
            return false;
        }
    }
    Serial.println("Image transmission successful.");
    return true;
}

bool initCamera() {
    camera_config_t config;
    config.ledc_channel = LEDC_CHANNEL_0;
    config.ledc_timer = LEDC_TIMER_0;
    config.pin_d0 = Y2_GPIO_NUM;  config.pin_d1 = Y3_GPIO_NUM;
    config.pin_d2 = Y4_GPIO_NUM;  config.pin_d3 = Y5_GPIO_NUM;
    config.pin_d4 = Y6_GPIO_NUM;  config.pin_d5 = Y7_GPIO_NUM;
    config.pin_d6 = Y8_GPIO_NUM;  config.pin_d7 = Y9_GPIO_NUM;
    config.pin_xclk = XCLK_GPIO_NUM;   config.pin_pclk = PCLK_GPIO_NUM;
    config.pin_vsync = VSYNC_GPIO_NUM; config.pin_href = HREF_GPIO_NUM;
    config.pin_sccb_sda = SIOD_GPIO_NUM; config.pin_sccb_scl = SIOC_GPIO_NUM;
    config.pin_pwdn = PWDN_GPIO_NUM;   config.pin_reset = RESET_GPIO_NUM;
    config.xclk_freq_hz = 20000000;
    config.pixel_format = PIXFORMAT_JPEG;
    if (psramFound()) {
        config.frame_size = CAPTURE_FRAMESIZE;
        config.jpeg_quality = CAPTURE_JPEG_QUALITY;
        config.fb_count = 1;
        config.fb_location = CAMERA_FB_IN_PSRAM;
    } else {
        Serial.println("WARNING: PSRAM not found - falling back to QVGA");
        config.frame_size = FRAMESIZE_QVGA;
        config.jpeg_quality = 18;
        config.fb_count = 1;
        config.fb_location = CAMERA_FB_IN_DRAM;
    }
    config.grab_mode = CAMERA_GRAB_WHEN_EMPTY;
    esp_err_t result = esp_camera_init(&config);
    if (result != ESP_OK) {
        Serial.print("Camera init failed: 0x");
        Serial.println(result, HEX);
        return false;
    }
    Serial.println("Camera initialized");
    return true;
}

bool initESPNow() {
    WiFi.mode(WIFI_STA);
    delay(100);
    esp_err_t result = esp_wifi_set_channel(ESPNOW_CHANNEL, WIFI_SECOND_CHAN_NONE);
    if (result != ESP_OK) {
        Serial.print("Failed to set WiFi channel: ");
        Serial.println(result);
        return false;
    }
    result = esp_now_init();
    if (result != ESP_OK) {
        Serial.print("ESP-NOW init failed: ");
        Serial.println(result);
        return false;
    }
    // Encryption. Both keys are exactly 16 bytes; config.h enforces the length
    // at compile time below. A wrong key on either side means the receiver
    // decrypts garbage and drops it - no picture leaves the air readable.
    static_assert(sizeof(ESPNOW_PMK) == 17, "ESPNOW_PMK must be exactly 16 characters");
    static_assert(sizeof(ESPNOW_LMK) == 17, "ESPNOW_LMK must be exactly 16 characters");
    result = esp_now_set_pmk((const uint8_t *)ESPNOW_PMK);
    if (result != ESP_OK) {
        Serial.print("esp_now_set_pmk failed: ");
        Serial.println(result);
        return false;
    }
    esp_now_register_recv_cb(onDataRecv);

    esp_now_peer_info_t peer = {};
    memcpy(peer.peer_addr, RECEIVER, 6);
    peer.channel = ESPNOW_CHANNEL;
    peer.encrypt = true;
    memcpy(peer.lmk, ESPNOW_LMK, 16);
    result = esp_now_add_peer(&peer);
    if (result != ESP_OK && result != ESP_ERR_ESPNOW_EXIST) {
        Serial.print("Failed to add receiver peer: ");
        Serial.println(result);
        return false;
    }
    return true;
}

void setup() {
    Serial.begin(115200);
    delay(1000);
    Serial.println();
    Serial.println("==============================");
    Serial.println("ESP32-CAM ESP-NOW TRANSMITTER");
    Serial.println("==============================");

    uint8_t mac[6];
    if (esp_read_mac(mac, ESP_MAC_WIFI_STA) == ESP_OK) {
        // Paste this into the receiver's ALLOWED_TRANSMITTERS.
        printMac("Transmitter MAC: ", mac);
    }

    if (!initCamera()) {
        Serial.println("Camera initialization FAILED");
        while (true) delay(1000);
    }
    if (!initESPNow()) {
        Serial.println("ESP-NOW initialization FAILED");
        while (true) delay(1000);
    }
    printMac("Receiver: ", RECEIVER);
    Serial.print("ESP-NOW channel: ");
    Serial.println(ESPNOW_CHANNEL);
    Serial.println("Link: encrypted (PMK+LMK)");
    Serial.println("Transmitter ready.");
}

void loop() {
    Serial.println();
    Serial.println("Capturing...");
    camera_fb_t *fb = esp_camera_fb_get();
    if (fb == nullptr) {
        Serial.println("Camera capture FAILED");
        delay(CAPTURE_INTERVAL_MS);
        return;
    }
    bool valid = fb->format == PIXFORMAT_JPEG && fb->len >= 4 &&
                 fb->buf[0] == 0xFF && fb->buf[1] == 0xD8 &&
                 fb->buf[fb->len - 2] == 0xFF && fb->buf[fb->len - 1] == 0xD9;
    if (!valid) {
        Serial.println("Invalid JPEG");
        esp_camera_fb_return(fb);
        delay(CAPTURE_INTERVAL_MS);
        return;
    }
    Serial.print("Captured ");
    Serial.print(fb->len);
    Serial.println(" bytes");

    if (!transmitImage(fb->buf, fb->len)) {
        Serial.println("Image transmission FAILED.");
    }
    esp_camera_fb_return(fb);
    delay(CAPTURE_INTERVAL_MS);
}
