import asyncio
from bleak import BleakClient

BATTERIES = {
    "Battery100-Babich": "A5:C2:37:5F:0E:17",
    "Babich-2": "A5:C2:39:4A:F7:45",
}

FF01 = "0000ff01-0000-1000-8000-00805f9b34fb"
FF02 = "0000ff02-0000-1000-8000-00805f9b34fb"

COMMANDS = [
    ("JBD Basic 0x03", bytes.fromhex("dd a5 03 00 ff fd 77")),
    ("JBD Cells 0x04", bytes.fromhex("dd a5 04 00 ff fc 77")),
    ("JBD HW Info 0x05", bytes.fromhex("dd a5 05 00 ff fb 77")),
    ("JBD Auth Default (123456)", bytes.fromhex("dd 5a 00 02 56 78 ff 30 77")),
    ("Alt 0x5A Basic", bytes.fromhex("5a a5 03 00 ff fd 77")),
]

async def deep_probe(name: str, mac: str):
    print(f"\n=== Глибока перевірка {name} ({mac}) ===")
    async with BleakClient(mac, timeout=12.0) as client:
        # 1. Спробуємо напряму прочитати ff01, ff02 та 2a50
        for uuid in [FF01, FF02, "00002a50-0000-1000-8000-00805f9b34fb"]:
            try:
                val = await client.read_gatt_char(uuid)
                print(f"  [READ {uuid[:8]}]: {val.hex(' ')} | ascii={val}")
            except Exception as e:
                print(f"  [READ {uuid[:8]} помилка]: {e}")

        # 2. Підписуємось ТІЛЬКИ на ff01
        def on_ff01(_, data: bytearray):
            print(f"  [NOTIFY ff01]: {data.hex(' ')}")

        await client.start_notify(FF01, on_ff01)

        # 3. Надсилаємо команди і після кожної додатково робимо read(ff01)
        for label, cmd in COMMANDS:
            print(f"  -> {label}: {cmd.hex(' ')}")
            await client.write_gatt_char(FF02, cmd, response=False)
            await asyncio.sleep(0.8)
            try:
                val = await client.read_gatt_char(FF01)
                if val:
                    print(f"     [READ після запису]: {val.hex(' ')}")
            except Exception:
                pass

        await client.stop_notify(FF01)

async def main():
    for name, mac in BATTERIES.items():
        try:
            await deep_probe(name, mac)
        except Exception as e:
            print(f"Помилка {name}: {e}")

asyncio.run(main())