// Copy to config.h in this folder and fill in. config.h is gitignored.
#pragma once

// Must match the transmitter.
#define ESPNOW_CHANNEL 6

// Same two 16-character keys as the transmitter's config.h.
#define ESPNOW_PMK "CHANGE-ME-16-CHR"
#define ESPNOW_LMK "CHANGE-ME-16-CHR"

// The transmitters this receiver will listen to, by MAC (each camera prints
// "Transmitter MAC: ..." at boot). Anything else on the channel is dropped
// before it is even parsed - an unlisted board cannot feed the node, key or
// no key. Up to 6 encrypted peers is the ESP-NOW limit.
#define ALLOWED_TRANSMITTERS { \
    {0x00, 0x00, 0x00, 0x00, 0x00, 0x00}, \
}

// USB serial to the Pi. The Python side must use the same number.
#define SERIAL_BAUD 921600
