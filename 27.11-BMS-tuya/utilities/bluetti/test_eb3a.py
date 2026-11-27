import asyncio
import struct
from bleak import BleakClient

MAC = "D7:55:00:C5:CE:CA"
FF01 = "0000ff01-0000-1000-8000-00805f9b34fb"
FF02 = "0000ff02-0000-1000-8000-00805f9b34fb"

def modbus_crc16(data: bytes) -> bytes:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 0x0001:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return struct.pack("<H", crc)

def build_read_cmd(start_reg: int, count: int) -> bytes:
    payload = struct.pack(">BBHH", 0x01, 0x03, start_reg, count)
    return payload + modbus_crc16(payload)

async def main():
    buf = bytearray()
    done = asyncio.Event()
    expected_bytes = 0

    def on_notify(_, data: bytearray):
        nonlocal expected_bytes
        buf.extend(data)
        if len(buf) >= 3 and expected_bytes == 0:
            expected_bytes = 3 + buf[2] + 2
        if expected_bytes > 0 and len(buf) >= expected_bytes:
            done.set()

    async with BleakClient(MAC, timeout=12.0) as client:
        await client.start_notify(FF01, on_notify)
        await asyncio.sleep(0.4)

        # Запит основних регістрів 0x000A..0x0031 (40 регістрів)
        cmd = build_read_cmd(0x000A, 40)
        await client.write_gatt_char(FF02, cmd, response=False)
        await asyncio.wait_for(done.wait(), timeout=6.0)
        await client.stop_notify(FF01)

    if len(buf) >= 85 and buf[0] == 0x01 and buf[1] == 0x03:
        regs = struct.unpack(">40H", buf[3:83])
        # Зміщення відносно 0x000A (10):
        dc_in = regs[36 - 10]   # 0x0024
        ac_in = regs[37 - 10]   # 0x0025
        ac_out = regs[38 - 10]  # 0x0026
        dc_out = regs[39 - 10]  # 0x0027
        soc = regs[43 - 10]     # 0x002B

        print(f"SOC (заряд): {soc}%")
        print(f"Вхід AC (розетка): {ac_in} Вт | Вхід DC (сонце/авто): {dc_in} Вт")
        print(f"Вихід AC (220 В): {ac_out} Вт | Вихід DC (USB/12В): {dc_out} Вт")
    else:
        print("Сира відповідь:", buf.hex(" "))

if __name__ == "__main__":
    asyncio.run(main())