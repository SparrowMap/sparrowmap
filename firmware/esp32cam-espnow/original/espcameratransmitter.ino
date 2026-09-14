#include <Arduino.h>
#include <WiFi.h>
#include <esp_now.h>
#include <esp_wifi.h>
#include "esp_camera.h"

// ============================================================
// SETTINGS
// ============================================================

#define ESPNOW_CHANNEL 6

#define CAPTURE_INTERVAL_MS 5000

// ESP-NOW v1 max packet = 250 bytes
// 11 bytes reserved for our header
#define MAX_PAYLOAD 220

#define ACK_TIMEOUT_MS 200
#define MAX_RETRIES 8

// ============================================================
// RECEIVER MAC ADDRESS
// ============================================================
//
// Replace these six bytes with the MAC printed by your
// receiver ESP32.
//
// Example:
// AA:BB:CC:DD:EE:FF
//
// becomes:
// {0xAA, 0xBB, 0xCC, 0xDD, 0xEE, 0xFF}
//

uint8_t RECEIVER_MAC[] = {
    0xAC,
    0x27,
    0x6E,
    0xA4,
    0xD3,
    0x38
};

// ============================================================
// CAMERA PINS
// AI Thinker ESP32-CAM
// ============================================================

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

// ============================================================
// ACK STATE
// ============================================================

volatile bool ackReceived = false;
volatile uint32_t ackFrame = 0;
volatile uint16_t ackSeq = 0;

// ============================================================
// FRAME COUNTER
// ============================================================

uint32_t frameCounter = 0;

// ============================================================
// CRC32
// ============================================================

uint32_t crc32(
    const uint8_t *data,
    size_t length
)
{
    uint32_t crc = 0xFFFFFFFF;

    for (size_t i = 0; i < length; i++) {

        crc ^= data[i];

        for (uint8_t j = 0; j < 8; j++) {

            if (crc & 1) {
                crc = (crc >> 1) ^ 0xEDB88320;
            } else {
                crc >>= 1;
            }
        }
    }

    return crc ^ 0xFFFFFFFF;
}

// ============================================================
// ESP-NOW RECEIVE CALLBACK
//
// Receives ACKs from the receiver.
// ============================================================

void onDataRecv(
    const esp_now_recv_info_t *info,
    const uint8_t *data,
    int len
)
{
    if (data == nullptr) {
        return;
    }

    // ACK is exactly 7 bytes
    if (len != 7) {
        return;
    }

    // ACK packet type
    if (data[0] != 0x02) {
        return;
    }

    // Decode frame ID
    uint32_t frameId =
        ((uint32_t)data[1]) |
        ((uint32_t)data[2] << 8) |
        ((uint32_t)data[3] << 16) |
        ((uint32_t)data[4] << 24);

    // Decode sequence
    uint16_t seq =
        ((uint16_t)data[5]) |
        ((uint16_t)data[6] << 8);

    ackFrame = frameId;
    ackSeq = seq;
    ackReceived = true;
}

// ============================================================
// SEND ONE IMAGE PACKET
//
// Packet layout:
//
// Byte 0       type
// Bytes 1-4    frame ID
// Bytes 5-6    sequence
// Bytes 7-8    total packets
// Bytes 9-10   payload length
// Bytes 11...  JPEG data
//
// Header = exactly 11 bytes
// Maximum packet = 231 bytes with 220-byte payload
// ============================================================

bool sendPacketReliably(
    uint32_t frameId,
    uint16_t seq,
    uint16_t total,
    const uint8_t *payload,
    uint16_t payloadLen
)
{
    if (payloadLen > MAX_PAYLOAD) {
        Serial.println("ERROR: Payload too large");
        return false;
    }

    uint8_t packet[11 + MAX_PAYLOAD];

    // --------------------------------------------------------
    // Packet type
    // --------------------------------------------------------

    packet[0] = 0x01;

    // --------------------------------------------------------
    // Frame ID - little endian
    // --------------------------------------------------------

    packet[1] = (frameId >> 0) & 0xFF;
    packet[2] = (frameId >> 8) & 0xFF;
    packet[3] = (frameId >> 16) & 0xFF;
    packet[4] = (frameId >> 24) & 0xFF;

    // --------------------------------------------------------
    // Sequence number
    // --------------------------------------------------------

    packet[5] = (seq >> 0) & 0xFF;
    packet[6] = (seq >> 8) & 0xFF;

    // --------------------------------------------------------
    // Total packets
    // --------------------------------------------------------

    packet[7] = (total >> 0) & 0xFF;
    packet[8] = (total >> 8) & 0xFF;

    // --------------------------------------------------------
    // Payload length
    // --------------------------------------------------------

    packet[9] = (payloadLen >> 0) & 0xFF;
    packet[10] = (payloadLen >> 8) & 0xFF;

    // --------------------------------------------------------
    // JPEG data
    // --------------------------------------------------------

    memcpy(
        packet + 11,
        payload,
        payloadLen
    );

    uint16_t packetLen = 11 + payloadLen;

    // Safety check
    if (packetLen > 250) {
        Serial.println("ERROR: ESP-NOW packet too large");
        return false;
    }

    // --------------------------------------------------------
    // Retry loop
    // --------------------------------------------------------

    for (int attempt = 1; attempt <= MAX_RETRIES; attempt++) {

        ackReceived = false;
        ackFrame = 0;
        ackSeq = 0;

        esp_err_t result = esp_now_send(
            RECEIVER_MAC,
            packet,
            packetLen
        );

        if (result != ESP_OK) {

            Serial.print("esp_now_send error: ");
            Serial.println(result);

            delay(10);
            continue;
        }

        unsigned long start = millis();

        while (millis() - start < ACK_TIMEOUT_MS) {

            if (ackReceived &&
                ackFrame == frameId &&
                ackSeq == seq)
            {
                return true;
            }

            delay(1);
        }

        Serial.print("Retry packet ");
        Serial.print(seq);
        Serial.print(" (attempt ");
        Serial.print(attempt);
        Serial.println(")");
    }

    return false;
}

// ============================================================
// TRANSMIT COMPLETE IMAGE
// ============================================================

bool transmitImage(
    const uint8_t *image,
    size_t imageSize
)
{
    uint32_t frameId = ++frameCounter;

    uint16_t totalPackets =
        (imageSize + MAX_PAYLOAD - 1) / MAX_PAYLOAD;

    uint32_t imageCRC =
        crc32(image, imageSize);

    Serial.println();
    Serial.println("------------------------------");

    Serial.print("Frame: ");
    Serial.println(frameId);

    Serial.print("JPEG size: ");
    Serial.print(imageSize);
    Serial.println(" bytes");

    Serial.print("Packets: ");
    Serial.println(totalPackets);

    Serial.print("CRC32: ");
    Serial.println(imageCRC, HEX);

    // --------------------------------------------------------
    // Send each packet
    // --------------------------------------------------------

    for (uint16_t seq = 0; seq < totalPackets; seq++) {

        size_t offset =
            (size_t)seq * MAX_PAYLOAD;

        size_t remaining =
            imageSize - offset;

        uint16_t payloadLen =
            remaining > MAX_PAYLOAD
                ? MAX_PAYLOAD
                : remaining;

        bool success = sendPacketReliably(
            frameId,
            seq,
            totalPackets,
            image + offset,
            payloadLen
        );

        if (!success) {

            Serial.print("FAILED at packet ");
            Serial.println(seq);

            return false;
        }
    }

    Serial.println("Image transmission successful.");

    return true;
}

// ============================================================
// CAMERA INITIALIZATION
// ============================================================

bool initCamera()
{
    camera_config_t config;

    config.ledc_channel = LEDC_CHANNEL_0;
    config.ledc_timer = LEDC_TIMER_0;

    config.pin_d0 = Y2_GPIO_NUM;
    config.pin_d1 = Y3_GPIO_NUM;
    config.pin_d2 = Y4_GPIO_NUM;
    config.pin_d3 = Y5_GPIO_NUM;
    config.pin_d4 = Y6_GPIO_NUM;
    config.pin_d5 = Y7_GPIO_NUM;
    config.pin_d6 = Y8_GPIO_NUM;
    config.pin_d7 = Y9_GPIO_NUM;

    config.pin_xclk = XCLK_GPIO_NUM;
    config.pin_pclk = PCLK_GPIO_NUM;
    config.pin_vsync = VSYNC_GPIO_NUM;
    config.pin_href = HREF_GPIO_NUM;

    config.pin_sccb_sda = SIOD_GPIO_NUM;
    config.pin_sccb_scl = SIOC_GPIO_NUM;

    config.pin_pwdn = PWDN_GPIO_NUM;
    config.pin_reset = RESET_GPIO_NUM;

    config.xclk_freq_hz = 20000000;

    config.pixel_format = PIXFORMAT_JPEG;

    // --------------------------------------------------------
    // Use PSRAM when available
    // --------------------------------------------------------

    if (psramFound()) {

        config.frame_size = FRAMESIZE_VGA;
        config.jpeg_quality = 15;
        config.fb_count = 1;
        config.fb_location = CAMERA_FB_IN_PSRAM;

    } else {

        Serial.println("WARNING: PSRAM not found");

        config.frame_size = FRAMESIZE_QVGA;
        config.jpeg_quality = 18;
        config.fb_count = 1;
        config.fb_location = CAMERA_FB_IN_DRAM;
    }

    config.grab_mode = CAMERA_GRAB_WHEN_EMPTY;

    esp_err_t result =
        esp_camera_init(&config);

    if (result != ESP_OK) {

        Serial.print("Camera init failed: 0x");
        Serial.println(result, HEX);

        return false;
    }

    Serial.println("Camera initialized");

    return true;
}

// ============================================================
// INITIALIZE ESP-NOW
// ============================================================

bool initESPNow()
{
    WiFi.mode(WIFI_STA);

    delay(100);

    // Force channel
    esp_err_t result =
        esp_wifi_set_channel(
            ESPNOW_CHANNEL,
            WIFI_SECOND_CHAN_NONE
        );

    if (result != ESP_OK) {

        Serial.print("Failed to set WiFi channel: ");
        Serial.println(result);

        return false;
    }

    // --------------------------------------------------------
    // Initialize ESP-NOW
    // --------------------------------------------------------

    result = esp_now_init();

    if (result != ESP_OK) {

        Serial.print("ESP-NOW init failed: ");
        Serial.println(result);

        return false;
    }

    // --------------------------------------------------------
    // Register ACK callback
    // --------------------------------------------------------

    esp_now_register_recv_cb(onDataRecv);

    // --------------------------------------------------------
    // Add receiver peer
    // --------------------------------------------------------

    esp_now_peer_info_t peerInfo = {};

    memcpy(
        peerInfo.peer_addr,
        RECEIVER_MAC,
        6
    );

    peerInfo.channel = ESPNOW_CHANNEL;
    peerInfo.encrypt = false;

    result = esp_now_add_peer(&peerInfo);

    if (result != ESP_OK &&
        result != ESP_ERR_ESPNOW_EXIST)
    {
        Serial.print("Failed to add receiver peer: ");
        Serial.println(result);

        return false;
    }

    return true;
}

// ============================================================
// SETUP
// ============================================================

void setup()
{
    Serial.begin(115200);

    delay(1000);

    Serial.println();
    Serial.println("==============================");
    Serial.println("ESP32-CAM ESP-NOW TRANSMITTER");
    Serial.println("==============================");

    // --------------------------------------------------------
    // Camera
    // --------------------------------------------------------

    if (!initCamera()) {

        Serial.println("Camera initialization FAILED");

        while (true) {
            delay(1000);
        }
    }

    // --------------------------------------------------------
    // ESP-NOW
    // --------------------------------------------------------

    if (!initESPNow()) {

        Serial.println("ESP-NOW initialization FAILED");

        while (true) {
            delay(1000);
        }
    }

    Serial.print("ESP-NOW channel: ");
    Serial.println(ESPNOW_CHANNEL);

    Serial.println("Transmitter ready.");
}

// ============================================================
// MAIN LOOP
// ============================================================

void loop()
{
    Serial.println();
    Serial.println("Capturing...");

    camera_fb_t *fb =
        esp_camera_fb_get();

    if (fb == nullptr) {

        Serial.println("Camera capture FAILED");

        delay(CAPTURE_INTERVAL_MS);

        return;
    }

    // --------------------------------------------------------
    // Basic JPEG validation
    // --------------------------------------------------------

    if (fb->format != PIXFORMAT_JPEG ||
        fb->len < 4)
    {
        Serial.println("Invalid JPEG");

        esp_camera_fb_return(fb);

        delay(CAPTURE_INTERVAL_MS);

        return;
    }

    // JPEG should begin FF D8
    bool validStart =
        fb->buf[0] == 0xFF &&
        fb->buf[1] == 0xD8;

    // JPEG should end FF D9
    bool validEnd =
        fb->buf[fb->len - 2] == 0xFF &&
        fb->buf[fb->len - 1] == 0xD9;

    if (!validStart || !validEnd) {

        Serial.println("JPEG markers invalid");

        esp_camera_fb_return(fb);

        delay(CAPTURE_INTERVAL_MS);

        return;
    }

    Serial.print("Captured ");
    Serial.print(fb->len);
    Serial.println(" bytes");

    // --------------------------------------------------------
    // Transmit
    // --------------------------------------------------------

    bool success =
        transmitImage(
            fb->buf,
            fb->len
        );

    if (!success) {
        Serial.println("Image transmission FAILED.");
    }

    // --------------------------------------------------------
    // Release camera framebuffer
    // --------------------------------------------------------

    esp_camera_fb_return(fb);

    // --------------------------------------------------------
    // Wait before next capture
    // --------------------------------------------------------

    delay(CAPTURE_INTERVAL_MS);
}