import asyncio
import struct
from bleak import BleakClient

BATTERIES = [
    {"name": "Battery100-Babich", "mac": "A5:C2:37:5F:0E:17"},
    {"name": "Babich-2", "mac": "A5:C2:39:4A:F7:45"},
]

FF01 = "0000ff01-0000-1000-8000-00805f9b34fb"
FF02 = "0000ff02-0000-1000-8000-00805f9b34fb"
CMD_BASIC = bytes.fromhex("dd a5 03 00 ff fd 77")
CMD_CELLS = bytes.fromhex("dd a5 04 00 ff fc 77")


async def send_jbd_cmd(client: BleakClient, cmd: bytes) -> bytes:
    buf = bytearray()
    done = asyncio.Event()

    def on_notify(_, data: bytearray):
        buf.extend(data)
        # JBD пакет: [0]=0xDD, [1]=cmd, [2]=status, [3]=payload_len, ..., [-1]=0x77
        if len(buf) >= 7:
            expected_len = buf[3] + 7
            if len(buf) >= expected_len and buf[-1] == 0x77:
                done.set()

    await client.start_notify(FF01, on_notify)
    await asyncio.sleep(0.35)  # Обов'язкова пауза для стабілізації BLE notify у Windows
    await client.write_gatt_char(FF02, cmd, response=False)
    await asyncio.wait_for(done.wait(), timeout=5.0)
    await client.stop_notify(FF01)
    return bytes(buf)


async def read_battery(name: str, mac: str) -> dict:
    async with BleakClient(mac, timeout=12.0) as client:
        basic = await send_jbd_cmd(client, CMD_BASIC)
        cells_raw = await send_jbd_cmd(client, CMD_CELLS)

    voltage = struct.unpack(">H", basic[4:6])[0] / 100.0
    current = struct.unpack(">h", basic[6:8])[0] / 100.0
    rem_ah = struct.unpack(">H", basic[8:10])[0] / 100.0
    full_ah = struct.unpack(">H", basic[10:12])[0] / 100.0
    cycles = struct.unpack(">H", basic[12:14])[0]
    soc = basic[23]
    fet_status = basic[24]  # біт 0 = заряд дозволено, біт 1 = розряд дозволено
    ntc_count = basic[26]

    temps = []
    for i in range(ntc_count):
        raw_t = struct.unpack(">H", basic[27 + i * 2 : 29 + i * 2])[0]
        temps.append(round((raw_t - 2731) / 10.0, 1))

    num_cells = cells_raw[3] // 2
    cells_v = [
        round(struct.unpack(">H", cells_raw[4 + i * 2 : 6 + i * 2])[0] / 1000.0, 3)
        for i in range(num_cells)
    ]

    return {
        "name": name,
        "mac": mac,
        "soc": soc,
        "voltage": voltage,
        "current": current,
        "power_w": round(voltage * current, 1),
        "remaining_ah": rem_ah,
        "full_ah": full_ah,
        "cycles": cycles,
        "charge_on": bool(fet_status & 0x01),
        "discharge_on": bool(fet_status & 0x02),
        "temps_c": temps,
        "cells_v": cells_v,
    }


async def main():
    for b in BATTERIES:
        try:
            info = await read_battery(b["name"], b["mac"])
            print(f"\n🔋 {info['name']}:")
            print(f"   Заряд (SoC):  {info['soc']}% ({info['remaining_ah']} / {info['full_ah']} А·год)")
            print(f"   Напруга/Струм: {info['voltage']} В  |  {info['current']} А ({info['power_w']} Вт)")
            print(f"   Комірки (4S): {info['cells_v']} В (розбаланс {round((max(info['cells_v']) - min(info['cells_v'])) * 1000)} мВ)")
            print(f"   Температура:  {info['temps_c']} °C  |  Циклів: {info['cycles']}")
        except Exception as e:
            print(f"\n❌ Помилка читання {b['name']}: {e}")


if __name__ == "__main__":
    asyncio.run(main())