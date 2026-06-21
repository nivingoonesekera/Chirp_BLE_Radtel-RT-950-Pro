"""BLE <-> serial bridge for the Radtel RT-950 Pro (replaces ble-serial).

Why this exists instead of ble-serial:
The radio refuses all clone traffic on ffe1 until it receives a one-time
"unlock" write on a *second* characteristic, ff31 (captured in gattattack.txt).
ble-serial can only drive a single write characteristic, so it can never both
unlock (ff31) and carry data (ffe1).  This bridge does the unlock once at
connect, then transparently pipes ffe1 <-> a serial port.

The unlock challenge is replayed verbatim from the capture; the radio's response
is deterministic and a replay is accepted (verified with ble_probe.py unlock),
so no live challenge computation is needed.

The radio drops the BLE link right after it commits the APRS write block (0x58)
to flash. That happens after the block is acknowledged, so the clone is already
complete; the bridge detects the disconnect, logs it with a timestamp, and
reconnects + re-unlocks so the next CHIRP attempt works without restarting.

Topology (unchanged from before):
    radio  <--BLE-->  ble_bridge.py  <--COM10|COM11(com0com)-->  CHIRP

Usage:
    python ble_bridge.py COM10 AA:BB:CC:DD:EE:FF      # COM port + radio MAC
    python ble_bridge.py COM10 AA:BB:CC:DD:EE:FF -v   # verbose: every frame as hex
    python ble_bridge.py COM10 AA:BB:CC:DD:EE:FF --fast
                                     # opt into the throughput-optimized 7.5 ms
                                     # interval: faster reads, but known to
                                     # corrupt/drop the APRS 0x58 block. Default
                                     # (no flag) is the slow, reliable interval.

    The MAC can also be set once via the RT950_BLE_ADDR environment variable.

Output is line-buffered, so you see [bridge] lines live in your terminal as
they happen (no need for `python -u`).

Copyright (c) 2026 Nivin Goonesekera - VK3NWG. MIT License (see LICENSE).
Part of the Radtel RT-950 Pro BLE CHIRP driver project.
"""

import asyncio
import os
import sys
import time

import serial
from bleak import BleakClient

# Flush each line immediately so progress shows live in any console.
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

ARGS = [a for a in sys.argv[1:]]
VERBOSE = "-v" in ARGS or "--verbose" in ARGS
# Connection-interval policy. The radio's BLE module is cheap and its preferred
# interval is slow (100-200 ms, per its 0x2a04 value). Requesting Windows'
# "throughput-optimized" 7.5 ms interval speeds up reads a lot, BUT the module
# can't hold that pace through the APRS flash commit and the link corrupts/drops
# on the final 0x58 block. The slow interval is the reliable one, so DEFAULT IS
# SLOW (reliable); pass --fast to opt into the throughput-optimized interval.
FAST = "--fast" in ARGS
# Adaptive write pacing (the speed knob).
#
# Two ways to push a 132-byte block over ffe1:
#   * write-WITHOUT-response: blast the ~7 chunks back to back, no per-chunk
#     ATT round-trip. Fast (block time collapses from ~1.3 s to a few hundred
#     ms) but no backpressure.
#   * write-WITH-response: wait for each chunk's ATT confirmation before the
#     next, like the USB cable's flow control. Slow but lets the radio's small
#     UART buffer drain to the MCU — needed through a flash commit.
#
# DEFAULT IS ADAPTIVE: bulk channel/settings blocks go write-without-response
# (fast), and ONLY the APRS frame (the flash commit) is sent write-with-response
# (gentle). Overrides:
#   --rsp    force write-WITH-response for every frame (old reliable behavior;
#            use this if bulk write-without-response proves flaky on your link)
#   --norsp  force write-WITHOUT-response for every frame (fastest, incl. APRS)
FORCE_RSP = "--rsp" in ARGS
FORCE_NORSP = "--norsp" in ARGS


def _looks_like_mac(s: str) -> bool:
    return s.count(":") == 5


# Positional args: the COM port (e.g. COM10) and the radio's BLE MAC. The MAC
# can also come from the RT950_BLE_ADDR environment variable. Order between the
# two positionals doesn't matter — the one with colons is the MAC.
_positional = [a for a in ARGS if not a.startswith("-")]
PORT = next((a for a in _positional if not _looks_like_mac(a)), "COM10")
ADDR = os.environ.get("RT950_BLE_ADDR") or next(
    (a for a in _positional if _looks_like_mac(a)), None
)
if not ADDR:
    sys.exit(
        "[bridge] No radio BLE address given.\n"
        "  Pass it as an argument or set RT950_BLE_ADDR, e.g.:\n"
        "      python ble_bridge.py COM10 AA:BB:CC:DD:EE:FF\n"
        "      set RT950_BLE_ADDR=AA:BB:CC:DD:EE:FF  (then: python ble_bridge.py COM10)\n"
        "  Find the MAC with any BLE scanner app, or just use the no-bridge\n"
        "  driver radtel_rt950pro_BLE_int.py, which scans and lets you pick the radio."
    )

# Command byte of the APRS *write* frame, used to recognise the flash-commit
# block on the wire so the bridge can pace it gently. Keep this in sync with the
# APRS CloneSegment.write_command in radtel_rt950pro_BL.py.
APRS_WRITE_CMD = 0x58  # confirmed by aprs_probe.py (0x55 was a wrong USB guess)
APRS_MARKER = bytes((APRS_WRITE_CMD, 0x00, 0x00, 0x80))


def _write_mode_for(is_aprs: bool) -> bool:
    """Return response= for write_gatt_char, honoring the adaptive policy."""
    if FORCE_RSP:
        return True
    if FORCE_NORSP:
        return False
    return is_aprs  # default: gentle (with response) only for the APRS frame

NOTIFY_UUID = "0000ffe1-0000-1000-8000-00805f9b34fb"  # notify IN  (radio -> PC)
DATA_UUID   = "0000ffe1-0000-1000-8000-00805f9b34fb"  # write OUT  (PC -> radio)
UNLOCK_UUID = "0000ff31-0000-1000-8000-00805f9b34fb"  # one-time unlock (write)

# Fixed unlock frame from gattattack.txt; replay is accepted by the radio.
UNLOCK = bytes.fromhex("3F3F3F3F022E171D5E57252F57136256044B2342")

# Keep the connection-parameters request object alive for the whole session;
# the fast interval stays in effect only while this reference exists.
_conn_param_request = None


def _ts() -> str:
    return time.strftime("%H:%M:%S")


def _request_fast_connection(client):
    """Ask Windows for the fastest BLE connection interval (the Android trick).

    Each 132-byte clone block is delivered as ~7 notifications (MTU is pinned at
    23 by the radio), and the gap between notifications is the connection
    interval. Windows' default interval is slow/power-saving; requesting
    ThroughputOptimized drops it toward 7.5 ms (the same thing Android does),
    which is the biggest single read-speed gain. Best effort: if the API isn't
    available we keep the default interval.
    """
    global _conn_param_request
    try:
        from winrt.windows.devices.bluetooth import (
            BluetoothLEPreferredConnectionParameters as P,
        )

        device = client._backend._requester  # underlying WinRT BluetoothLEDevice
        _conn_param_request = device.request_preferred_connection_parameters(
            P.throughput_optimized
        )
        status = getattr(_conn_param_request, "status", None)
        print(f"[bridge] requested throughput-optimized connection (status={status})")
    except Exception as exc:  # pragma: no cover - platform/version dependent
        print(f"[bridge] could not request fast connection ({exc!r}); using default interval")


async def connect_and_unlock(ser, stats, disconnected: asyncio.Event):
    """Connect, verify the GATT table, start notify, unlock. Returns the client.

    Retries the connection: the radio's GATT table occasionally comes back
    incomplete on a fresh connection, so verify ffe1/ff31 are present and
    reconnect if not.
    """

    def on_disconnect(_client):
        t0 = stats.get("aprs_t0")
        extra = f" ({time.monotonic() - t0:.2f}s after APRS block sent)" if t0 else ""
        print(f"[bridge] !!! {_ts()} radio dropped the BLE connection{extra}")
        disconnected.set()

    def on_notify(_char, data: bytearray):
        # Swallow the unlock response (always starts with 0x21 "!"): it is the
        # radio answering the ff31 challenge and is NOT part of the clone
        # stream. Forwarding it would leave a stale 0x21 in the serial buffer
        # and break CHIRP's first handshake. Only do this until CHIRP has sent
        # its first byte: after that, a notify chunk that happens to begin
        # with 0x21 is legitimate clone data and must pass through.
        if data and data[0] == 0x21 and stats["tx"] == 0:
            if VERBOSE:
                print(f"  radio->  (unlock reply, swallowed) {bytes(data).hex().upper()}")
            return
        stats["rx"] += len(data)
        stats["notifies"] += 1
        # After the APRS (0x58) block goes out, log every byte the radio sends
        # back, with timing, so its ACK (or a drop) is visible in the trace.
        t0 = stats.get("aprs_t0")
        if t0 is not None:
            print(f"  [APRS] {_ts()} radio->PC +{time.monotonic() - t0:.3f}s "
                  f"[{len(data):3}] {bytes(data).hex().upper()}")
        elif VERBOSE:
            print(f"  radio->PC [{len(data):3}] {bytes(data).hex().upper()}")
        # radio -> PC: hand straight to the serial port for CHIRP to read
        try:
            ser.write(bytes(data))
        except serial.SerialTimeoutException:
            # CHIRP isn't draining COM11 yet; drop rather than stall the loop.
            pass

    for attempt in range(1, 6):
        print(f"[bridge] connecting to {ADDR} (attempt {attempt}) ...")
        client = BleakClient(ADDR, disconnected_callback=on_disconnect)
        try:
            await client.connect()
        except Exception as exc:
            print(f"[bridge] connect failed ({exc}); retrying ...")
            await asyncio.sleep(1.5)
            continue
        notify_ch = client.services.get_characteristic(NOTIFY_UUID)
        unlock_ch = client.services.get_characteristic(UNLOCK_UUID)
        if notify_ch is not None and unlock_ch is not None:
            print(f"[bridge] connected (mtu={client.mtu_size}); ffe1+ff31 present")
            break
        print("[bridge] GATT table incomplete (ffe1/ff31 missing), reconnecting ...")
        await client.disconnect()
        await asyncio.sleep(1.0)
    else:
        raise SystemExit("[bridge] could not get a complete GATT table after 5 tries")

    if FAST:
        print("[bridge] --fast: requesting throughput-optimized interval "
              "(faster reads, but known to corrupt the APRS 0x58 block)")
        _request_fast_connection(client)
    else:
        print("[bridge] using the radio's default (slow) connection interval "
              "for reliability — like ble-serial")
    await client.start_notify(notify_ch, on_notify)

    print(f"[bridge] sending unlock on ff31 ...")
    await client.write_gatt_char(UNLOCK_UUID, UNLOCK, response=True)
    await asyncio.sleep(0.4)
    print(f"[bridge] {_ts()} unlock sent; bridge live. Run the clone in CHIRP. Ctrl+C to stop.")
    return client, notify_ch


async def pipe(client, ser, stats, disconnected: asyncio.Event):
    """Pump CHIRP's serial bytes to the radio until the link drops."""
    chunk = max(20, client.mtu_size - 3)
    loop = asyncio.get_event_loop()
    last_report = time.monotonic()
    while not disconnected.is_set():
        # PC -> radio: drain whatever CHIRP wrote to the serial port
        data = await loop.run_in_executor(None, ser.read, 256)
        if data:
            stats["tx"] += len(data)
            # Spotlight the APRS write: CHIRP sends the block as header
            # <cmd> 00 00 80 + 128 bytes. Mark the moment it goes on the air so
            # we can time the radio's reply (or its silence + drop) against it,
            # and pace this one frame gently regardless of the bulk write mode.
            is_aprs = APRS_MARKER in data
            response = _write_mode_for(is_aprs)
            if is_aprs:
                stats["aprs_t0"] = time.monotonic()
                print(f"\n[bridge] === {_ts()} APRS block (0x{APRS_WRITE_CMD:02X}) -> radio, "
                      f"{len(data)} bytes in {(len(data)+chunk-1)//chunk} chunks "
                      f"(write {'WITH' if response else 'WITHOUT'} response) ===")
            if VERBOSE and not is_aprs:
                print(f"  PC->radio [{len(data):3}] {bytes(data).hex().upper()}")
            try:
                for i in range(0, len(data), chunk):
                    await client.write_gatt_char(
                        DATA_UUID, data[i:i + chunk], response=response
                    )
                    if is_aprs:
                        print(f"  [APRS] {_ts()} chunk {i//chunk} "
                              f"({len(data[i:i+chunk])} B) write returned OK")
            except Exception as exc:
                t0 = stats.get("aprs_t0")
                where = f" (APRS chunk, +{time.monotonic()-t0:.3f}s)" if t0 else ""
                print(
                    f"[bridge] !!! {_ts()} GATT write failed mid-frame{where} "
                    f"({exc}); {len(data)} byte(s) from CHIRP lost"
                )
                disconnected.set()
                break
        else:
            await asyncio.sleep(0.005)
        # Live throughput line once a second while traffic is flowing.
        now = time.monotonic()
        if now - last_report >= 1.0 and (stats["rx"] or stats["tx"]):
            print(
                f"[bridge] live: radio->PC {stats['rx']} B in {stats['notifies']} pkts, "
                f"PC->radio {stats['tx']} B"
            )
            last_report = now


async def main():
    ser = serial.Serial(PORT, baudrate=115200, timeout=0, write_timeout=2.0)
    print(f"[bridge] opened {PORT}")
    if FORCE_RSP:
        mode = "WITH response for every frame (paced/reliable)"
    elif FORCE_NORSP:
        mode = "WITHOUT response for every frame (fastest)"
    else:
        mode = (f"adaptive — bulk WITHOUT response (fast), APRS frame "
                f"(0x{APRS_WRITE_CMD:02X}) WITH response (gentle)")
    print(f"[bridge] ffe1 write mode: {mode}")

    while True:
        stats = {"rx": 0, "tx": 0, "notifies": 0}
        disconnected = asyncio.Event()
        client = None
        try:
            client, notify_ch = await connect_and_unlock(ser, stats, disconnected)
            await pipe(client, ser, stats, disconnected)
        except (KeyboardInterrupt, asyncio.CancelledError):
            break
        finally:
            if client is not None:
                try:
                    await client.stop_notify(notify_ch)
                except Exception:
                    pass
                try:
                    await client.disconnect()
                except Exception:
                    pass
        # Radio-side drop: flush whatever half-frame is stuck in the serial
        # buffers (the clone attempt is dead anyway) and bring the link back
        # so the user's next CHIRP attempt just works.
        try:
            ser.reset_input_buffer()
            ser.reset_output_buffer()
        except Exception:
            pass
        print(f"[bridge] {_ts()} link lost; reconnecting in 2 s (Ctrl+C to stop) ...")
        await asyncio.sleep(2.0)

    ser.close()
    print("\n[bridge] closed")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
