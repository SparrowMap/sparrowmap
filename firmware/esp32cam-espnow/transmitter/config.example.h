// Copy to config.h in this folder and fill in. config.h is gitignored:
// the keys below are secrets for YOUR link and never belong in the repo.
#pragma once

// 2.4 GHz channel both boards sit on. Must match the receiver.
#define ESPNOW_CHANNEL 6

// ---- KEYS (exactly 16 characters each) ----------------------------------
// ESP-NOW encrypts with a Primary Master Key (shared by every board on the
// link) and a Local Master Key per peer. Without BOTH matching, the receiver
// cannot decrypt a frame and silently drops it - which is the point: a still
// photograph of a public road with readable plates in it must not cross the
// air in the clear, and nobody without the key can inject pictures into the
// node. Generate two random 16-character strings and paste them into both
// config.h files.  e.g.  python -c "import secrets;print(secrets.token_urlsafe(12)[:16])"
#define ESPNOW_PMK "CHANGE-ME-16-CHR"
#define ESPNOW_LMK "CHANGE-ME-16-CHR"

// MAC of the receiver board, printed on its serial console at boot
// ("Receiver MAC: AA:BB:CC:DD:EE:FF" -> {0xAA,0xBB,0xCC,0xDD,0xEE,0xFF}).
#define RECEIVER_MAC {0xAC, 0x27, 0x6E, 0xA4, 0xD3, 0x38}

// ---- CAMERA ----------------------------------------------------------------
// Milliseconds between captures. Each VGA frame is ~150 packets and takes
// 1-2 s to send with per-packet ACKs, so the real cadence is interval + that.
#define CAPTURE_INTERVAL_MS 5000

// FRAMESIZE_VGA (640x480, ~30 KB, ~1.5 s over the air) or FRAMESIZE_QVGA
// (320x240, ~8 KB, ~0.4 s). QVGA sends four times as often; VGA reads plates
// further away. Pick for the spot the camera watches.
#define CAPTURE_FRAMESIZE FRAMESIZE_VGA
#define CAPTURE_JPEG_QUALITY 15   // 10 (best) .. 63 (smallest)
