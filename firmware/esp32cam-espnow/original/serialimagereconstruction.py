import serial
import struct
import zlib
import os
import time

# Lucian H
# Serial image reconstruction system for esp-now image bridge
# Code will decode the esp-now image packets and save them as jpgs


# ============================================================
# SETTINGS
# ============================================================

SERIAL_PORT = "/dev/ttyUSB0"
# Change this to the serial port that the esp-32 is connected to. 
#Do ls /dev/ttyUSB*, disconnect the esp32 and do the command again. The serial port that
#disappeared is your esp32 connection
SERIAL_BAUD = 921600

OUTPUT_DIR = "camera"
LATEST_FILE = os.path.join(OUTPUT_DIR, "latest.jpg")

SAVE_HISTORY = True
HISTORY_DIR = os.path.join(OUTPUT_DIR, "history")

# Our ESP32 serial header:
#
# 4s  = "SPKT"
# I   = frame ID
# H   = sequence
# H   = total packets
# H   = payload length
# I   = image length
# I   = image CRC
#
HEADER = struct.Struct("<4sIHHHII")

MAGIC = b"SPKT"


# ============================================================
# SETUP
# ============================================================

os.makedirs(OUTPUT_DIR, exist_ok=True)

if SAVE_HISTORY:
    os.makedirs(HISTORY_DIR, exist_ok=True)


# ============================================================
# READ EXACT NUMBER OF BYTES
# ============================================================

def read_exact(ser, count):

    data = bytearray()

    while len(data) < count:

        chunk = ser.read(count - len(data))

        if not chunk:
            raise TimeoutError("Serial timeout")

        data.extend(chunk)

    return bytes(data)


# ============================================================
# WAIT FOR SPKT
# ============================================================

def find_magic(ser):

    buffer = bytearray()

    while True:

        byte = ser.read(1)

        if not byte:
            raise TimeoutError("Waiting for SPKT")

        buffer += byte

        if len(buffer) > len(MAGIC):
            buffer.pop(0)

        if bytes(buffer) == MAGIC:
            return


# ============================================================
# SAVE IMAGE
# ============================================================

def save_image(image, frame_id):

    # --------------------------------------------------------
    # JPEG sanity check
    # --------------------------------------------------------

    if len(image) < 4:
        print("Image too small")
        return False

    if image[0:2] != b"\xFF\xD8":
        print("Invalid JPEG start marker")
        return False

    if image[-2:] != b"\xFF\xD9":
        print("Invalid JPEG end marker")
        return False

    # --------------------------------------------------------
    # Write atomically
    # --------------------------------------------------------

    temp_file = LATEST_FILE + ".tmp"

    with open(temp_file, "wb") as f:
        f.write(image)
        f.flush()
        os.fsync(f.fileno())

    os.replace(
        temp_file,
        LATEST_FILE
    )

    # --------------------------------------------------------
    # Optional history
    # --------------------------------------------------------

    if SAVE_HISTORY:

        history_file = os.path.join(
            HISTORY_DIR,
            f"frame_{frame_id:08d}.jpg"
        )

        with open(history_file, "wb") as f:
            f.write(image)

    print(
        f"Saved {LATEST_FILE} "
        f"({len(image)} bytes)"
    )

    return True


# ============================================================
# RECEIVE ONE IMAGE
# ============================================================

def receive_image(ser):

    # --------------------------------------------------------
    # Find beginning of a serial packet
    # --------------------------------------------------------

    find_magic(ser)

    # We already consumed SPKT.
    # Read the remaining 18 bytes.
    remaining_header = read_exact(
        ser,
        HEADER.size - 4
    )

    header = (
        MAGIC +
        remaining_header
    )

    (
        magic,
        frame_id,
        seq,
        total,
        payload_len,
        image_len,
        image_crc
    ) = HEADER.unpack(header)

    # --------------------------------------------------------
    # Validate header
    # --------------------------------------------------------

    if magic != MAGIC:
        return None

    if total == 0:
        print("Invalid total packet count")
        return None

    if seq >= total:
        print(
            f"Invalid sequence {seq}/{total}"
        )
        return None

    if payload_len > 220:
        print(
            f"Invalid payload length: {payload_len}"
        )
        return None

    # --------------------------------------------------------
    # Read JPEG chunk
    # --------------------------------------------------------

    payload = read_exact(
        ser,
        payload_len
    )

    return {
        "frame_id": frame_id,
        "seq": seq,
        "total": total,
        "payload": payload,
        "image_len": image_len,
        "image_crc": image_crc
    }


# ============================================================
# RECEIVE COMPLETE FRAME
# ============================================================

def receive_frame(ser, first_packet):

    frame_id = first_packet["frame_id"]
    total = first_packet["total"]

    packets = {}

    # --------------------------------------------------------
    # Store first packet
    # --------------------------------------------------------

    packets[first_packet["seq"]] = (
        first_packet["payload"]
    )

    expected_image_len = (
        first_packet["image_len"]
    )

    expected_crc = (
        first_packet["image_crc"]
    )

    print(
        f"Receiving frame {frame_id} "
        f"({total} packets)"
    )

    # --------------------------------------------------------
    # Receive remaining packets
    # --------------------------------------------------------

    while len(packets) < total:

        packet = receive_image(ser)

        if packet is None:
            continue

        # ----------------------------------------------------
        # Make sure it belongs to this frame
        # ----------------------------------------------------

        if packet["frame_id"] != frame_id:

            print(
                f"Ignoring frame "
                f"{packet['frame_id']}"
            )

            continue

        # ----------------------------------------------------
        # Store packet
        # ----------------------------------------------------

        packets[packet["seq"]] = (
            packet["payload"]
        )

        print(
            f"\rPackets: "
            f"{len(packets)}/{total}",
            end="",
            flush=True
        )

    print()

    # --------------------------------------------------------
    # Make sure every sequence exists
    # --------------------------------------------------------

    for seq in range(total):

        if seq not in packets:

            print(
                f"Missing packet {seq}"
            )

            return None

    # --------------------------------------------------------
    # Reassemble
    # --------------------------------------------------------

    image = b"".join(
        packets[seq]
        for seq in range(total)
    )

    print(
        f"Reassembled image: "
        f"{len(image)} bytes"
    )

    # --------------------------------------------------------
    # Verify image length if available
    # --------------------------------------------------------

    if expected_image_len != 0:

        if len(image) != expected_image_len:

            print(
                f"IMAGE LENGTH ERROR: "
                f"expected {expected_image_len}, "
                f"got {len(image)}"
            )

            return None

    # --------------------------------------------------------
    # CRC32
    # --------------------------------------------------------

    calculated_crc = (
        zlib.crc32(image) & 0xFFFFFFFF
    )

    print(
        f"CRC32: "
        f"{calculated_crc:08X}"
    )

    if expected_crc != 0:

        if calculated_crc != expected_crc:

            print(
                f"CRC ERROR: "
                f"expected {expected_crc:08X}, "
                f"got {calculated_crc:08X}"
            )

            return None

    # --------------------------------------------------------
    # Save
    # --------------------------------------------------------

    if not save_image(
        image,
        frame_id
    ):
        return None

    return image


# ============================================================
# MAIN
# ============================================================

def main():

    print(
        f"Opening {SERIAL_PORT} "
        f"at {SERIAL_BAUD} baud..."
    )

    ser = serial.Serial(
        SERIAL_PORT,
        SERIAL_BAUD,
        timeout=2
    )

    # ESP32 may reset when USB serial opens.
    time.sleep(2)

    print("Waiting for images...")
    print()

    while True:

        try:

            # ------------------------------------------------
            # Find first packet
            # ------------------------------------------------

            first = receive_image(ser)

            if first is None:
                continue

            # ------------------------------------------------
            # Receive complete frame
            # ------------------------------------------------

            receive_frame(
                ser,
                first
            )

        except TimeoutError:

            # Just keep waiting for data
            continue

        except KeyboardInterrupt:

            print()
            print("Stopping...")
            break

        except Exception as e:

            print(
                f"Error: {e}"
            )

            time.sleep(1)

    ser.close()


if __name__ == "__main__":
    main()