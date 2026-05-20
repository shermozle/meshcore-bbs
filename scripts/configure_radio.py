#!/usr/bin/env python3
"""One-shot radio configuration script.

Run this while the BBS container is stopped to set the companion's radio
parameters over USB serial. Settings persist in the device's flash.

Usage (from repo root, container must be stopped):
    docker run --rm \
      --device=/dev/serial/by-id/YOUR_DEVICE:/dev/ttyACM0 \
      --group-add <dialout-gid> \
      meshcore-bbs:latest \
      python scripts/configure_radio.py

Edit FREQ, BW, SF, CR below before running.
"""

import asyncio
import sys

SERIAL_PATH = "/dev/ttyACM0"
BAUD        = 115200

FREQ = 915.075   # MHz
BW   = 125.0     # kHz
SF   = 9
CR   = 5


async def main() -> int:
    from meshcore import MeshCore

    print(f"Connecting to {SERIAL_PATH} ...")
    mc = await MeshCore.create_serial(SERIAL_PATH, BAUD)
    if mc is None:
        print("ERROR: no response from device — is companion firmware running?")
        return 1

    name = (mc.self_info or {}).get("name", "unknown")
    pubkey = (mc.self_info or {}).get("public_key", "")[:12]
    print(f"Connected: {name} ({pubkey})")
    print(f"Setting radio: freq={FREQ} MHz  bw={BW} kHz  SF={SF}  CR={CR}")

    result = await mc.commands.set_radio(FREQ, BW, SF, CR)
    if result.is_error():
        print(f"ERROR: set_radio failed: {result.payload}")
        await mc.disconnect()
        return 1

    print("Radio configured OK")

    # Read back device info to confirm.
    info = await mc.commands.send_appstart()
    if not info.is_error():
        p = info.payload
        print(f"Confirmed: freq={p.get('radio_freq')} MHz  "
              f"bw={p.get('radio_bw')} kHz  "
              f"SF={p.get('radio_sf')}  CR={p.get('radio_cr')}")

    await mc.disconnect()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
