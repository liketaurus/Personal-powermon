import asyncio
import json
import os
import platform
import re
import struct
import subprocess
import sys
import time
from datetime import datetime
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

try:
    from bleak import BleakClient
    HAS_BLEAK = True
except ImportError:
    HAS_BLEAK = False

try:
    import tinytuya
    HAS_TINYTUYA = True
except ImportError:
    HAS_TINYTUYA = False

app = FastAPI(title="Poltava Power Monitor")

KYIV_TZ = ZoneInfo("Europe/Kyiv")
MAIN_PAGE_URL = "https://www.poe.pl.ua/disconnection/power-outages/"
POE_URL = "https://www.poe.pl.ua/customs/newgpv-info.php"
CACHE_TTL_SECONDS = 300

# Налаштування датчика 220 В: WAN-адреса роутера Taurus у мережі основного роутера (192.168.1.1)
TAURUS_WAN_IP = "192.168.1.53"

# Налаштування батарей: дві Keepworth (JBD Smart BMS) + зарядна станція Bluetti EB3A
BATTERIES_CONFIG = [
    {"id": "cabinet", "room": "Кабінет", "type": "jbd", "mac": "A5:C2:37:5F:0E:17"},
    {"id": "kitchen", "room": "Кухня", "type": "jbd", "mac": "A5:C2:39:4A:F7:45"},
    {"id": "bedroom", "room": "Спальня", "type": "bluetti", "mac": "D7:55:00:C5:CE:CA", "capacity_wh": 268.0},
]

FF01 = "0000ff01-0000-1000-8000-00805f9b34fb"
FF02 = "0000ff02-0000-1000-8000-00805f9b34fb"
CMD_BASIC = bytes.fromhex("dd a5 03 00 ff fd 77")

# Шлях до статичних файлів (всередині _MEIPASS для PyInstaller або поруч зі скриптом)
BASE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
STATIC_DIR = os.path.join(BASE_DIR, "static")

# Шлях для збереження історії фактичних відключень, кешу та конфігу Tuya (поруч із .exe або app.py)
APP_DIR = (
    os.path.dirname(sys.executable)
    if getattr(sys, "frozen", False)
    else os.path.dirname(os.path.abspath(__file__))
)
HISTORY_FILE = os.path.join(APP_DIR, "actual_outages.json")
SCHEDULE_CACHE_FILE = os.path.join(APP_DIR, "schedule_cache.json")
TUYA_CONFIG_FILE = os.path.join(APP_DIR, "tuya_config.json")

_cache: Dict[str, Dict[str, Any]] = {}
_current_power_state: Optional[bool] = None
_batteries_state: List[Dict[str, Any]] = [
    {
        "id": b["id"],
        "room": b["room"],
        "type": b["type"],
        "soc": None,
        "voltage": None,
        "current_a": None,
        "rem_ah": None,
        "rem_wh": None,
        "power_w": None,
        "online": False,
    }
    for b in BATTERIES_CONFIG
]

_climate_state: Dict[str, Any] = {
    "configured": False,
    "online": False,
    "temp_in": None,
    "humidity": None,
    "temp_ext": None,
    "battery_state": None,
    "updated_at": None,
}


def load_tuya_config() -> Optional[Dict[str, Any]]:
    if not os.path.exists(TUYA_CONFIG_FILE):
        return None
    try:
        with open(TUYA_CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        if not cfg.get("enabled", True):
            return None
        if not (cfg.get("api_key") and cfg.get("api_secret") and cfg.get("device_id")):
            return None
        return cfg
    except Exception:
        return None


def fetch_tuya_climate_sync(cfg: Dict[str, Any]) -> Dict[str, Any]:
    cloud = tinytuya.Cloud(
        apiRegion=cfg.get("region", "eu"),
        apiKey=cfg["api_key"],
        apiSecret=cfg["api_secret"],
        apiDeviceID=cfg["device_id"],
    )
    res = cloud.getstatus(cfg["device_id"])
    result_list = res.get("result")
    if not isinstance(result_list, list):
        raise ValueError(f"Помилка відповіді Tuya Cloud: {res}")

    raw = {item["code"]: item["value"] for item in result_list if "code" in item}
    temp_in = round(raw["temp_current"] / 10.0, 1) if "temp_current" in raw else None
    temp_ext = (
        round(raw["temp_current_external"] / 10.0, 1)
        if "temp_current_external" in raw
        else None
    )
    humidity = int(raw["humidity_value"]) if "humidity_value" in raw else None
    battery_state = raw.get("battery_state")

    return {
        "temp_in": temp_in,
        "humidity": humidity,
        "temp_ext": temp_ext,
        "battery_state": battery_state,
    }


async def tuya_watchdog_loop():
    """Фонове опитування термометра Tuya кожні 120 секунд."""
    while True:
        cfg = load_tuya_config()
        if not HAS_TINYTUYA or not cfg:
            _climate_state["configured"] = bool(cfg)
            _climate_state["online"] = False
            await asyncio.sleep(60)
            continue

        _climate_state["configured"] = True
        try:
            data = await asyncio.to_thread(fetch_tuya_climate_sync, cfg)
            _climate_state.update(
                {
                    "online": True,
                    "temp_in": data["temp_in"],
                    "humidity": data["humidity"],
                    "temp_ext": data["temp_ext"],
                    "battery_state": data["battery_state"],
                    "updated_at": datetime.now(KYIV_TZ).strftime("%H:%M"),
                }
            )
        except Exception:
            _climate_state["online"] = False

        await asyncio.sleep(120)


def check_taurus_ping() -> bool:
    """
    Перевіряє доступність роутера Taurus (192.168.1.53) через ICMP ping.
    Працює без root-прав на Android (Termux), Windows (.exe) та Linux.
    """
    is_windows = platform.system().lower() == "windows"
    cmd = (
        ["ping", "-n", "1", "-w", "1000", TAURUS_WAN_IP]
        if is_windows
        else ["ping", "-c", "1", "-W", "1", TAURUS_WAN_IP]
    )
    creationflags = subprocess.CREATE_NO_WINDOW if is_windows else 0

    try:
        res = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
            timeout=3,
        )
        return res.returncode == 0
    except Exception:
        return False


def load_outage_events() -> List[Dict[str, Any]]:
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []


def save_outage_events(events: List[Dict[str, Any]]) -> None:
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(events[-200:], f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def record_power_transition(is_online: bool) -> None:
    """Фіксує зміну стану 220 В у лог-файл actual_outages.json."""
    global _current_power_state
    now_iso = datetime.now(KYIV_TZ).isoformat(timespec="seconds")
    events = load_outage_events()

    if _current_power_state is None:
        _current_power_state = is_online
        if not is_online and (not events or events[-1].get("on_at") is not None):
            events.append({"off_at": now_iso, "on_at": None})
            save_outage_events(events)
        elif is_online and events and events[-1].get("on_at") is None:
            events[-1]["on_at"] = now_iso
            save_outage_events(events)
        return

    if _current_power_state and not is_online:
        events.append({"off_at": now_iso, "on_at": None})
        save_outage_events(events)
    elif not _current_power_state and is_online:
        if events and events[-1].get("on_at") is None:
            events[-1]["on_at"] = now_iso
        else:
            events.append({"off_at": None, "on_at": now_iso})
        save_outage_events(events)

    _current_power_state = is_online


def _modbus_crc16(data: bytes) -> bytes:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 0x0001:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return struct.pack("<H", crc)


def _build_modbus_read(start_reg: int, count: int) -> bytes:
    payload = struct.pack(">BBHH", 0x01, 0x03, start_reg, count)
    return payload + _modbus_crc16(payload)


async def read_single_jbd_battery(mac: str) -> Dict[str, Any]:
    buf = bytearray()
    done = asyncio.Event()

    def on_notify(_, data: bytearray):
        buf.extend(data)
        if len(buf) >= 7:
            expected_len = buf[3] + 7
            if len(buf) >= expected_len and buf[-1] == 0x77:
                done.set()

    async with BleakClient(mac, timeout=10.0) as client:
        await client.start_notify(FF01, on_notify)
        await asyncio.sleep(0.35)
        await client.write_gatt_char(FF02, CMD_BASIC, response=False)
        await asyncio.wait_for(done.wait(), timeout=5.0)
        await client.stop_notify(FF01)

    voltage = round(struct.unpack(">H", buf[4:6])[0] / 100.0, 2)
    current = round(struct.unpack(">h", buf[6:8])[0] / 100.0, 2)
    rem_ah = round(struct.unpack(">H", buf[8:10])[0] / 100.0, 1)
    soc = int(buf[23])
    return {
        "soc": soc,
        "voltage": voltage,
        "current_a": current,
        "rem_ah": rem_ah,
        "rem_wh": round(rem_ah * 12.8),
        "power_w": round(voltage * current),
    }


async def read_bluetti_eb3a(mac: str, capacity_wh: float = 268.0) -> Dict[str, Any]:
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

    async with BleakClient(mac, timeout=5.0) as client:
        await client.start_notify(FF01, on_notify)
        await asyncio.sleep(0.35)
        cmd = _build_modbus_read(0x000A, 40)
        await client.write_gatt_char(FF02, cmd, response=False)
        await asyncio.wait_for(done.wait(), timeout=5.0)
        await client.stop_notify(FF01)

    if len(buf) < 85 or buf[0] != 0x01 or buf[1] != 0x03:
        raise ValueError("Некоректна відповідь Modbus від Bluetti EB3A")

    regs = struct.unpack(">40H", buf[3:83])
    dc_in = regs[36 - 10]   # 0x0024
    ac_in = regs[37 - 10]   # 0x0025
    ac_out = regs[38 - 10]  # 0x0026
    dc_out = regs[39 - 10]  # 0x0027
    soc = int(regs[43 - 10])  # 0x002B

    total_in = ac_in + dc_in
    total_out = ac_out + dc_out
    net_power = total_in - total_out
    rem_wh = round(capacity_wh * (soc / 100.0))

    return {
        "soc": soc,
        "voltage": None,
        "current_a": None,
        "rem_ah": None,
        "rem_wh": rem_wh,
        "power_w": net_power,
    }


async def batteries_watchdog_loop():
    """Фонове опитування батарей Кабінет, Кухня та Bluetti EB3A у Спальні."""
    if not HAS_BLEAK:
        return
    while True:
        for idx, cfg in enumerate(BATTERIES_CONFIG):
            try:
                if cfg["type"] == "bluetti":
                    info = await read_bluetti_eb3a(
                        cfg["mac"], cfg.get("capacity_wh", 268.0)
                    )
                else:
                    info = await read_single_jbd_battery(cfg["mac"])

                _batteries_state[idx].update(
                    {
                        "soc": info["soc"],
                        "voltage": info["voltage"],
                        "current_a": info["current_a"],
                        "rem_ah": info["rem_ah"],
                        "rem_wh": info["rem_wh"],
                        "power_w": info["power_w"],
                        "online": True,
                    }
                )
            except Exception:
                _batteries_state[idx]["online"] = False
                if cfg["type"] == "bluetti":
                    _batteries_state[idx].update(
                        {
                            "soc": None,
                            "rem_wh": None,
                            "power_w": None,
                        }
                    )
            await asyncio.sleep(1.0)
        await asyncio.sleep(45)


async def taurus_watchdog_loop():
    """Фонова перевірка 192.168.1.53 кожні 30 секунд із записом у лог."""
    while True:
        online = await asyncio.to_thread(check_taurus_ping)
        record_power_transition(online)
        await asyncio.sleep(30)


@app.on_event("startup")
async def on_startup():
    global _cache
    if os.path.exists(SCHEDULE_CACHE_FILE):
        try:
            with open(SCHEDULE_CACHE_FILE, "r", encoding="utf-8") as f:
                _cache = json.load(f)
        except Exception:
            _cache = {}
    asyncio.create_task(taurus_watchdog_loop())
    asyncio.create_task(batteries_watchdog_loop())
    asyncio.create_task(tuya_watchdog_loop())


def parse_schedule_html(html: str, queue: str) -> List[int]:
    soup = BeautifulSoup(html, "html.parser")
    parts = queue.split(".")
    if len(parts) != 2:
        raise ValueError(f"Невірний формат черги: {queue}")
    main_q, sub_q = parts[0], parts[1]

    target_row = None
    current_main = None

    for tr in soup.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        if not cells:
            continue
        texts = [c.get_text(strip=True) for c in cells]

        m_main = re.match(r"^(\d+)\s*черга", texts[0], re.IGNORECASE)
        if m_main:
            current_main = m_main.group(1)
            if len(texts) > 1 and texts[1] == sub_q and current_main == main_q:
                target_row = tr
                break
        elif texts[0] == sub_q and current_main == main_q:
            target_row = tr
            break
        elif texts[0] == queue:
            target_row = tr
            break

    if not target_row:
        raise ValueError(f"Чергу {queue} не знайдено у відповіді poe.pl.ua")

    slots: List[int] = []
    for td in target_row.find_all("td"):
        classes = " ".join(td.get("class", [])).lower()
        style = (td.get("style") or "").lower()

        if (
            "gpv-s1" in classes
            or "light-on" in classes
            or "#a8d08d" in style
            or "green" in style
        ):
            slots.append(0)
        elif (
            "gpv-s2" in classes
            or "light-off" in classes
            or "#f4b084" in style
            or "#ff" in style
            or "red" in style
        ):
            slots.append(1)
        elif (
            "gpv-s3" in classes
            or "possible" in classes
            or "#ffd966" in style
            or "yellow" in style
        ):
            slots.append(2)

    if len(slots) != 48:
        raise ValueError(f"Очікувалось 48 слотів, розпізнано {len(slots)}")

    return slots


async def fetch_poe_schedule(date_str: str, queue: str) -> List[int]:
    cache_key = f"{date_str}_{queue}"
    now_ts = time.time()

    if cache_key in _cache and (now_ts - _cache[cache_key]["ts"] < CACHE_TTL_SECONDS):
        return _cache[cache_key]["slots"]

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept": "text/html, */*; q=0.01",
        "Accept-Language": "uk-UA,uk;q=0.9,en;q=0.8",
        "Referer": MAIN_PAGE_URL,
        "Origin": "https://www.poe.pl.ua",
        "X-Requested-With": "XMLHttpRequest",
    }

    async with httpx.AsyncClient(
        timeout=20.0, headers=headers, follow_redirects=True, verify=False
    ) as client:
        await client.get(MAIN_PAGE_URL)
        resp = await client.post(
            POE_URL,
            data={"seldate": f'{{"date_in":"{date_str}"}}'},
        )
        resp.raise_for_status()
        slots = parse_schedule_html(resp.text, queue)

    _cache[cache_key] = {"ts": now_ts, "slots": slots}
    try:
        with open(SCHEDULE_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(_cache, f, ensure_ascii=False)
    except Exception:
        pass

    return slots


@app.get("/api/schedule")
async def get_schedule(
    queue: str = Query("1.1", pattern=r"^[1-6]\.[1-2]$"),
    date: str = Query(None, pattern=r"^\d{2}-\d{2}-\d{4}$"),
    force: bool = Query(False),
):
    now = datetime.now(KYIV_TZ)
    target_date = date or now.strftime("%d-%m-%Y")

    if force:
        _cache.pop(f"{target_date}_{queue}", None)

    try:
        slots = await fetch_poe_schedule(target_date, queue)
        return {
            "status": "ok",
            "date": target_date,
            "queue": queue,
            "updated_at": now.strftime("%H:%M:%S"),
            "slots": slots,
        }
    except Exception as e:
        cache_key = f"{target_date}_{queue}"
        if cache_key in _cache:
            return {
                "status": "stale",
                "date": target_date,
                "queue": queue,
                "updated_at": "кеш",
                "slots": _cache[cache_key]["slots"],
                "warning": str(e),
            }
        return JSONResponse(
            status_code=502, content={"status": "error", "detail": str(e)}
        )


@app.get("/api/actual")
async def get_actual_power():
    """Повертає поточний стан роутера Taurus (192.168.1.53)."""
    return {
        "online": _current_power_state,
        "sensor_ip": TAURUS_WAN_IP,
    }


@app.get("/api/batteries")
async def get_batteries():
    """Повертає стан батарей Keepworth (Кабінет, Кухня) та Bluetti EB3A (Спальня) по BLE."""
    return {"ble_available": HAS_BLEAK, "batteries": _batteries_state}


@app.get("/api/climate")
async def get_climate(force: bool = Query(False)):
    """Повертає температуру всередині, вологість, температуру виносного зонда та стан батарейки Tuya."""
    if force and HAS_TINYTUYA:
        cfg = load_tuya_config()
        if cfg:
            try:
                data = await asyncio.to_thread(fetch_tuya_climate_sync, cfg)
                _climate_state.update(
                    {
                        "configured": True,
                        "online": True,
                        "temp_in": data["temp_in"],
                        "humidity": data["humidity"],
                        "temp_ext": data["temp_ext"],
                        "battery_state": data["battery_state"],
                        "updated_at": datetime.now(KYIV_TZ).strftime("%H:%M"),
                    }
                )
            except Exception:
                pass
    return _climate_state


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))