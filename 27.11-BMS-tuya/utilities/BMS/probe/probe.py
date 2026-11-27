import asyncio
from bleak import BleakClient

BATTERIES = {
    "Battery100-Babich": "A5:C2:37:5F:0E:17",
    "Babich-2": "A5:C2:39:4A:F7:45",
}

# Набір команд різних протоколів BMS
PROBES = [
    ("JBD (0xDD)", bytes.fromhex("dd a5 03 00 ff fd 77")),
    ("Daly (0xA5)", bytes.fromhex("a5 40 90 08 00 00 00 00 00 00 00 00 bd")),
    ("Tianpower / Pace (0x55)", bytes.fromhex("55 14 83 00 00 00 aa")),
    ("Pace Modbus (0x7E)", bytes.fromhex("7e 32 30 30 31 34 36 34 32 45 30 30 32 30 31 46 44 33 35 0d")),
]

async def probe_battery(name: str, mac: str):
    print(f"\n=== Перевірка {name} ({mac}) ===")
    try:
        async with BleakClient(mac, timeout=12.0) as client:
            notify_chars = []
            write_chars = []

            for service in client.services:
                print(f"Сервіс: {service.uuid}")
                for char in service.characteristics:
                    props = ",".join(char.properties)
                    print(f"  └─ Характеристика: {char.uuid} [{props}]")
                    if "notify" in char.properties or "indicate" in char.properties:
                        notify_chars.append(char.uuid)
                    if "write" in char.properties or "write-without-response" in char.properties:
                        write_chars.append(char.uuid)

            received = []

            def on_notify(sender, data: bytearray):
                print(f"  [ВІДПОВІДЬ від {sender}]: {data.hex(' ')} (len={len(data)})")
                received.append((sender, bytes(data)))

            for n_uuid in notify_chars:
                try:
                    await client.start_notify(n_uuid, on_notify)
                except Exception as e:
                    print(f"  Не вдалося підписатися на {n_uuid}: {e}")

            for proto_name, cmd in PROBES:
                for w_uuid in write_chars:
                    print(f"  Надсилаю {proto_name} -> {w_uuid}...")
                    try:
                        await client.write_gatt_char(w_uuid, cmd, response=False)
                        await asyncio.sleep(1.2)
                        if received:
                            print(f"  >>> ЗНАЙДЕНО РОБОЧИЙ ПРОТОКОЛ: {proto_name} на {w_uuid}!")
                            return
                    except Exception:
                        pass
    except Exception as e:
        print(f"Помилка підключення до {name}: {e}")

async def main():
    for name, mac in BATTERIES.items():
        await probe_battery(name, mac)

asyncio.run(main())