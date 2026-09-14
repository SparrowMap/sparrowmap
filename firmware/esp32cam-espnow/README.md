# ESP32-CAM over ESP-NOW: a camera with no network

Designed and built by **Lucian H ([@lucianbuilds](https://github.com/lucianbuilds))**,
September 2026. The first SparrowMap camera node not built by the project's author.
His own repo, with the build photos and his tutorial, is
[lucianbuilds/espnow-image-bridge](https://github.com/lucianbuilds/espnow-image-bridge).
The website guide built from it is [map.sparrowmap.com/espcam](https://map.sparrowmap.com/espcam).

An AI-Thinker ESP32-CAM takes a still every few seconds and sends it, in
220-byte ESP-NOW packets with per-packet acknowledgement and retry, to a
second ESP32 plugged into a Raspberry Pi's USB port. A Python script on the Pi
rebuilds the JPEG and keeps `camera/latest.jpg` fresh. The SparrowMap node on
the Pi reads that file like any other camera.

Why it fits SparrowMap: **the camera never joins a WiFi network and has no
route to the internet.** No SSID, no password, no cloud. Recognition, cropping
and the plate guarantee happen on the Pi, where every other node does them.

What it is good for: a spot with no WiFi and no wire. A checkpoint, a parked
patrol, a gate, a road end. Range is set by the receiver's antenna.

What it is not: a detector feed. A VGA frame is ~150 packets and takes 1 to 2
seconds to cross the air, plus the capture interval, so expect **one frame
every 6 to 7 seconds at VGA**, or about every 1.5 seconds at QVGA. A car passes
in 1 to 2 seconds. Treat it as a snapshot sentinel until the packet size grows
(ESP-NOW v2 on newer cores allows 1470-byte payloads; not used here yet).

## Parts

| role | board | notes |
|---|---|---|
| camera (transmitter) | AI-Thinker ESP32-CAM with OV2640 | Lucian used the Hosyond kit with the USB-UART dongle (Amazon B09TB1GJ7P). PSRAM on the board is what allows VGA. |
| receiver | any ESP32 | Lucian used an ESP32-S3 dev board **for its external IPEX antenna** — that antenna is the range. Any ESP32 with a good link to the camera works. |
| host | Raspberry Pi or any computer | runs `pi/espnow_bridge.py` and the SparrowMap node |

## Files

```
transmitter/transmitter.ino     the camera
transmitter/config.example.h    copy to config.h: keys, receiver MAC, capture settings
receiver/receiver.ino           the USB-serial bridge
receiver/config.example.h       copy to config.h: keys, allowed camera MACs, baud
pi/espnow_bridge.py             rebuilds JPEGs on the Pi -> camera/latest.jpg
original/                       Lucian's files exactly as received, for reference
```

`config.h` is gitignored in both sketch folders. **Never commit it.**

## Setup, in order

### 1. Arduino IDE

- Install the **esp32 by Espressif** board package, **version 3.x** (the sketches
  use the 3.x `esp_now_recv_info_t` receive callback).
- No extra libraries: `WiFi`, `esp_now`, `esp_camera` ship with the core.

### 2. Flash the receiver first (you need its MAC)

1. Copy `receiver/config.example.h` to `receiver/config.h`.
2. Board settings for a generic ESP32-S3 dev board: **Tools → Board → ESP32S3 Dev
   Module**, USB CDC On Boot: Enabled (so the serial console works over the USB
   port), everything else default. For a plain ESP32: **ESP32 Dev Module**.
   *(Lucian's exact receiver settings were cut off in the email that carried the
   code; these are the standard ones and they match the sketch.)*
3. Upload. Open Serial Monitor at **921600**. It prints:
   ```
   Receiver MAC: AC:27:6E:A4:D3:38
   WARNING: no transmitters in ALLOWED_TRANSMITTERS - nothing will be accepted
   ```
   Keep that MAC.

### 3. Make the keys (once per link)

ESP-NOW encrypts with two 16-character keys: a Primary Master Key shared by the
link and a Local Master Key per peer. Generate two random ones:

```
python -c "import secrets; print(secrets.token_urlsafe(12)[:16])"
python -c "import secrets; print(secrets.token_urlsafe(12)[:16])"
```

They go into **both** `config.h` files, identical. Sixteen characters exactly;
the sketch refuses to compile otherwise.

Why this is not optional: the picture on the air is a full still of a public
road with every plate readable. Without encryption anyone in 2.4 GHz range
can reassemble it, and the receiver would accept frames from any board that
learned the packet format. With the keys, a frame from a board without them
decrypts to garbage and is dropped; a board not in the receiver's allow-list is
dropped before it is parsed at all.

### 4. Flash the camera

1. Copy `transmitter/config.example.h` to `transmitter/config.h`. Fill in
   `ESPNOW_PMK`, `ESPNOW_LMK`, and `RECEIVER_MAC` from step 2 as
   `{0xAC, 0x27, 0x6E, 0xA4, 0xD3, 0x38}`.
2. Board settings: **Tools → Board → AI Thinker ESP32-CAM**. If that entry is
   missing, use **ESP32 Dev Module** with **PSRAM: Enabled** and **Partition
   Scheme: Huge APP (3MB No OTA)**. Upload speed 115200 is the safe choice with
   the UART dongle.
3. Wiring for the dongle: dongle 5V → 5V, GND → GND, TX → U0R, RX → U0T.
   **GPIO0 to GND while uploading**, then remove the jumper and press reset.
4. Upload. Serial Monitor at **115200**. It prints:
   ```
   Transmitter MAC: 24:6F:28:AA:BB:CC
   Camera initialized
   Link: encrypted (PMK+LMK)
   Transmitter ready.
   ```
   Keep that MAC.

### 5. Tell the receiver which cameras to trust

Put the camera's MAC into `receiver/config.h`:

```c
#define ALLOWED_TRANSMITTERS { \
    {0x24, 0x6F, 0x28, 0xAA, 0xBB, 0xCC}, \
}
```

Up to six cameras per receiver (ESP-NOW's encrypted-peer limit). Re-upload the
receiver. Its console now prints `Allowed transmitter: 24:6F:28:AA:BB:CC` and,
once the camera boots, chunks start flowing. Both boards must sit on the same
`ESPNOW_CHANNEL` (default 6).

### 6. The Pi

```
pip install pyserial
python3 pi/espnow_bridge.py --port /dev/ttyUSB0 --out camera
```

Find the port with `ls /dev/ttyUSB* /dev/ttyACM*`, unplug the receiver, list
again; the one that vanished is it. An S3 usually shows as `/dev/ttyACM0`.
The script prints one line per saved frame and keeps the last 200 in
`camera/history/` (`--history 0` to keep none).

### 7. SparrowMap

Point the node at the file the bridge keeps fresh:

```
python3 detect/run_live.py --source camera/latest.jpg
```

`FrameGrabber` recognises an image path and re-reads the file each time it
changes, so the node sees every new frame the moment it lands and nothing in
between. Enrol the node the usual way (`/contribute` on the hub); on the
contributor beta first, then live.

## Protocol

Over the air (ESP-NOW, encrypted):

| type | bytes | layout |
|---|---|---|
| `0x00` start | 15 | frame_id u32, total u16, image_len u32, crc32 u32 |
| `0x01` data | 11 + n | frame_id u32, seq u16, total u16, len u16, JPEG bytes |
| `0x02` ack | 7 | frame_id u32, seq u16 (`0xFFFF` acknowledges a start) |

Receiver to Pi over serial, per chunk: `SPKT` + `<IHHHII>` (frame_id, seq,
total, payload_len, image_len, image_crc) + payload. `image_len`/`image_crc`
are zero only if the receiver never saw that frame's start packet; the Pi then
saves on JPEG markers alone.

## Changes from Lucian's original

Kept: the design, the packet layout, the ACK/retry loop, the atomic write.
Changed, after review:

1. **Encryption on** (PMK + LMK) and a **receiver allow-list**. The original
   had `encrypt = false` and added any sender as a peer.
2. **The CRC is now sent.** The original computed CRC32 on the camera but never
   transmitted it, and the receiver forwarded zeros, so the Pi's check never ran.
3. **A newer frame abandons an unfinished one.** The original waited for the
   rest of a frame the camera had given up on, and because the serial line never
   went quiet it waited forever.
4. History capped; port, baud and output are arguments; keys in `config.h`.
