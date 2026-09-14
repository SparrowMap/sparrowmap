#include <Arduino.h>
#include <WiFi.h>
#include <esp_now.h>
#include <esp_wifi.h>
#include <esp_mac.h>

// ============================================================
// SETTINGS
// ============================================================

#define ESPNOW_CHANNEL 6

#define SERIAL_BAUD 921600

// Must match transmitter
#define MAX_PAYLOAD 220

#define RX_QUEUE_SIZE 20

// ============================================================
// PACKET TYPES
// ============================================================

#define PACKET_TYPE_IMAGE 0x01
#define PACKET_TYPE_ACK   0x02

// ============================================================
// SERIAL HEADER
//
// Python expects:
//
// <4sIHHHII
//
// Which is exactly:
//
// 4 + 4 + 2 + 2 + 2 + 4 + 4 = 22 bytes
//
// packed prevents C++ alignment padding.
// ============================================================

struct __attribute__((packed)) SerialPacketHeader {

    char magic[4];

    uint32_t frame_id;

    uint16_t seq;
    uint16_t total;
    uint16_t payload_len;

    uint32_t image_len;
    uint32_t image_crc;
};

static_assert(
    sizeof(SerialPacketHeader) == 22,
    "ERROR: SerialPacketHeader must be exactly 22 bytes"
);

// ============================================================
// ACK PACKET
// ============================================================

struct __attribute__((packed)) AckPacket {

    uint8_t type;

    uint32_t frame_id;

    uint16_t seq;
};

static_assert(
    sizeof(AckPacket) == 7,
    "ERROR: AckPacket must be exactly 7 bytes"
);

// ============================================================
// RECEIVED PACKET QUEUE ENTRY
// ============================================================

struct ReceivedPacket {

    uint8_t source_mac[6];

    uint16_t len;

    uint8_t data[250];
};

// ============================================================
// GLOBALS
// ============================================================

QueueHandle_t rxQueue = nullptr;

// ============================================================
// PRINT HARDWARE MAC
// ============================================================

void printHardwareMAC()
{
    uint8_t mac[6];

    esp_err_t result =
        esp_read_mac(
            mac,
            ESP_MAC_WIFI_STA
        );

    if (result != ESP_OK) {

        Serial.println(
            "ERROR: Could not read WiFi MAC"
        );

        return;
    }

    Serial.print("Receiver MAC: ");

    for (int i = 0; i < 6; i++) {

        if (mac[i] < 16) {
            Serial.print("0");
        }

        Serial.print(
            mac[i],
            HEX
        );

        if (i < 5) {
            Serial.print(":");
        }
    }

    Serial.println();
}

// ============================================================
// ENSURE ESP-NOW PEER EXISTS
// ============================================================

bool ensurePeer(
    const uint8_t *mac
)
{
    if (esp_now_is_peer_exist(mac)) {
        return true;
    }

    esp_now_peer_info_t peerInfo = {};

    memcpy(
        peerInfo.peer_addr,
        mac,
        6
    );

    peerInfo.channel = ESPNOW_CHANNEL;
    peerInfo.encrypt = false;

    esp_err_t result =
        esp_now_add_peer(&peerInfo);

    if (result == ESP_OK) {

        Serial.println(
            "Added ESP-NOW peer"
        );

        return true;
    }

    if (result == ESP_ERR_ESPNOW_EXIST) {
        return true;
    }

    Serial.print(
        "Failed to add ESP-NOW peer: "
    );

    Serial.println(result);

    return false;
}

// ============================================================
// ESP-NOW RECEIVE CALLBACK
//
// Keep this callback short.
// Copy packet into FreeRTOS queue.
// ============================================================

void onReceive(
    const esp_now_recv_info_t *info,
    const uint8_t *data,
    int len
)
{
    if (info == nullptr ||
        data == nullptr)
    {
        return;
    }

    if (len <= 0 ||
        len > 250)
    {
        return;
    }

    ReceivedPacket packet;

    memcpy(
        packet.source_mac,
        info->src_addr,
        6
    );

    packet.len = len;

    memcpy(
        packet.data,
        data,
        len
    );

    // Don't block inside callback.
    xQueueSend(
        rxQueue,
        &packet,
        0
    );
}

// ============================================================
// SEND ACK TO TRANSMITTER
// ============================================================

void sendAck(
    const uint8_t *destination,
    uint32_t frameId,
    uint16_t seq
)
{
    AckPacket ack;

    ack.type = PACKET_TYPE_ACK;

    ack.frame_id = frameId;

    ack.seq = seq;

    esp_err_t result =
        esp_now_send(
            destination,
            (uint8_t *)&ack,
            sizeof(ack)
        );

    if (result != ESP_OK) {

        Serial.print(
            "ACK send error: "
        );

        Serial.println(result);
    }
}

// ============================================================
// SEND CHUNK TO RASPBERRY PI
// ============================================================

void sendToPi(
    uint32_t frameId,
    uint16_t seq,
    uint16_t total,
    const uint8_t *payload,
    uint16_t payloadLen,
    uint32_t imageLen,
    uint32_t imageCRC
)
{
    SerialPacketHeader header;

    // Exactly 4 bytes
    memcpy(
        header.magic,
        "SPKT",
        4
    );

    header.frame_id = frameId;

    header.seq = seq;

    header.total = total;

    header.payload_len = payloadLen;

    header.image_len = imageLen;

    header.image_crc = imageCRC;

    // --------------------------------------------------------
    // Send EXACTLY 22-byte header
    // --------------------------------------------------------

    Serial.write(
        (const uint8_t *)&header,
        sizeof(header)
    );

    // --------------------------------------------------------
    // Send JPEG chunk
    // --------------------------------------------------------

    if (payloadLen > 0) {

        Serial.write(
            payload,
            payloadLen
        );
    }

    // --------------------------------------------------------
    // Wait until serial data has been transmitted
    // --------------------------------------------------------

    Serial.flush();
}

// ============================================================
// PROCESS IMAGE PACKET
// ============================================================

void processPacket(
    const ReceivedPacket &packet
)
{
    // --------------------------------------------------------
    // Minimum header size
    // --------------------------------------------------------

    if (packet.len < 11) {

        return;
    }

    const uint8_t *p =
        packet.data;

    // --------------------------------------------------------
    // Packet type
    // --------------------------------------------------------

    if (p[0] != PACKET_TYPE_IMAGE) {

        return;
    }

    // --------------------------------------------------------
    // Decode frame ID
    // --------------------------------------------------------

    uint32_t frameId =
        ((uint32_t)p[1]) |
        ((uint32_t)p[2] << 8) |
        ((uint32_t)p[3] << 16) |
        ((uint32_t)p[4] << 24);

    // --------------------------------------------------------
    // Sequence
    // --------------------------------------------------------

    uint16_t seq =
        ((uint16_t)p[5]) |
        ((uint16_t)p[6] << 8);

    // --------------------------------------------------------
    // Total packets
    // --------------------------------------------------------

    uint16_t total =
        ((uint16_t)p[7]) |
        ((uint16_t)p[8] << 8);

    // --------------------------------------------------------
    // Payload length
    // --------------------------------------------------------

    uint16_t payloadLen =
        ((uint16_t)p[9]) |
        ((uint16_t)p[10] << 8);

    // --------------------------------------------------------
    // Validate total
    // --------------------------------------------------------

    if (total == 0) {

        Serial.println(
            "Invalid packet: total = 0"
        );

        return;
    }

    // --------------------------------------------------------
    // Validate sequence
    // --------------------------------------------------------

    if (seq >= total) {

        Serial.println(
            "Invalid packet: sequence out of range"
        );

        return;
    }

    // --------------------------------------------------------
    // Validate payload size
    // --------------------------------------------------------

    if (payloadLen > MAX_PAYLOAD) {

        Serial.println(
            "Invalid packet: payload too large"
        );

        return;
    }

    // --------------------------------------------------------
    // Expected ESP-NOW packet length
    //
    // 11-byte header + payload
    // --------------------------------------------------------

    uint16_t expectedLen =
        11 + payloadLen;

    if (packet.len != expectedLen) {

        Serial.print(
            "Invalid image packet length. Expected "
        );

        Serial.print(
            expectedLen
        );

        Serial.print(
            ", got "
        );

        Serial.println(
            packet.len
        );

        return;
    }

    // --------------------------------------------------------
    // JPEG data begins at byte 11
    // --------------------------------------------------------

    const uint8_t *payload =
        packet.data + 11;

    // --------------------------------------------------------
    // Forward to Raspberry Pi
    // --------------------------------------------------------

    sendToPi(
        frameId,
        seq,
        total,
        payload,
        payloadLen,
        0,
        0
    );

    // --------------------------------------------------------
    // ACK only after forwarding to Pi
    // --------------------------------------------------------

    sendAck(
        packet.source_mac,
        frameId,
        seq
    );
}

// ============================================================
// SETUP
// ============================================================

void setup()
{
    Serial.begin(
        SERIAL_BAUD
    );

    delay(1000);

    Serial.println();

    Serial.println(
        "=============================="
    );

    Serial.println(
        "ESP-NOW RECEIVER"
    );

    Serial.println(
        "=============================="
    );

    // --------------------------------------------------------
    // WiFi station mode
    // --------------------------------------------------------

    WiFi.mode(WIFI_STA);

    delay(100);

    // --------------------------------------------------------
    // Force ESP-NOW channel
    // --------------------------------------------------------

    esp_err_t result =
        esp_wifi_set_channel(
            ESPNOW_CHANNEL,
            WIFI_SECOND_CHAN_NONE
        );

    if (result != ESP_OK) {

        Serial.print(
            "ERROR setting WiFi channel: "
        );

        Serial.println(result);
    }

    // --------------------------------------------------------
    // Print actual hardware MAC
    // --------------------------------------------------------

    printHardwareMAC();

    Serial.print(
        "ESP-NOW channel: "
    );

    Serial.println(
        ESPNOW_CHANNEL
    );

    Serial.print(
        "Serial baud: "
    );

    Serial.println(
        SERIAL_BAUD
    );

    Serial.print(
        "Serial header size: "
    );

    Serial.println(
        sizeof(SerialPacketHeader)
    );

    // --------------------------------------------------------
    // Create receive queue
    // --------------------------------------------------------

    rxQueue =
        xQueueCreate(
            RX_QUEUE_SIZE,
            sizeof(ReceivedPacket)
        );

    if (rxQueue == nullptr) {

        Serial.println(
            "ERROR: Failed to create RX queue"
        );

        while (true) {
            delay(1000);
        }
    }

    // --------------------------------------------------------
    // Initialize ESP-NOW
    // --------------------------------------------------------

    result =
        esp_now_init();

    if (result != ESP_OK) {

        Serial.print(
            "ERROR: ESP-NOW init failed: "
        );

        Serial.println(result);

        while (true) {
            delay(1000);
        }
    }

    // --------------------------------------------------------
    // Register receive callback
    // --------------------------------------------------------

    esp_now_register_recv_cb(
        onReceive
    );

    Serial.println(
        "ESP-NOW initialized"
    );

    Serial.println(
        "Waiting for transmitter..."
    );

    Serial.println();
}

// ============================================================
// MAIN LOOP
// ============================================================

void loop()
{
    ReceivedPacket packet;

    if (
        xQueueReceive(
            rxQueue,
            &packet,
            pdMS_TO_TICKS(100)
        ) == pdTRUE
    )
    {
        // ----------------------------------------------------
        // Make sure transmitter is a peer
        // ----------------------------------------------------

        if (!ensurePeer(
                packet.source_mac
            ))
        {
            return;
        }

        // ----------------------------------------------------
        // Process
        // ----------------------------------------------------

        processPacket(
            packet
        );
    }

    delay(1);
}