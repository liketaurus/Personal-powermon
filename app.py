import os
import re
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Dict, Any, List

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI(title="Poltava Power Monitor")

KYIV_TZ = ZoneInfo("Europe/Kyiv")
MAIN_PAGE_URL = "https://www.poe.pl.ua/disconnection/power-outages/"
POE_URL = "https://www.poe.pl.ua/customs/newgpv-info.php"
CACHE_TTL_SECONDS = 300

_cache: Dict[str, Dict[str, Any]] = {}


def extract_slots_from_cells(cells: list) -> List[int]:
    slot_cells = cells[-48:]
    slots: List[int] = []
    for cell in slot_cells:
        classes = cell.get("class", [])
        class_str = " ".join(classes) if isinstance(classes, list) else str(classes)
        if "gpv-s2" in class_str or "light_2" in class_str:
            slots.append(1)  # Немає світла
        elif "gpv-s3" in class_str or "light_3" in class_str:
            slots.append(2)  # Можливе відключення
        else:
            slots.append(0)  # Є світло (gpv-s1 / light_1)
    return slots


def parse_poe_html(html: str, target_queue: str) -> List[int]:
    soup = BeautifulSoup(html, "html.parser")
    rows = soup.find_all("tr")

    main_q, sub_q = target_queue.split(".")
    current_main_queue = None

    for tr in rows:
        cells = tr.find_all(["td", "th"])
        if len(cells) < 48:
            continue

        meta_cells = cells[:-48]
        meta_texts = [c.get_text(strip=True) for c in meta_cells]
        joined_meta = " ".join(meta_texts)

        if re.search(rf"(^|\D){re.escape(target_queue)}(\D|$)", joined_meta):
            return extract_slots_from_cells(cells)

        if len(meta_cells) >= 2:
            m_main = re.search(r"\b([1-6])\b", meta_texts[0])
            if m_main:
                current_main_queue = m_main.group(1)
            if current_main_queue == main_q and re.search(rf"(^|\D){re.escape(sub_q)}(\D|$)", meta_texts[1]):
                return extract_slots_from_cells(cells)
        elif len(meta_cells) == 1 and current_main_queue == main_q:
            if re.search(rf"(^|\D){re.escape(sub_q)}(\D|$)", meta_texts[0]):
                return extract_slots_from_cells(cells)

    raise ValueError(f"Чергу {target_queue} не знайдено у відповіді Полтаваобленерго (довжина HTML: {len(html)})")


async def raw_poe_request() -> Dict[str, Any]:
    now_kyiv = datetime.now(KYIV_TZ)
    date_str = now_kyiv.strftime("%d-%m-%Y")

    base_headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "uk-UA,uk;q=0.9,en;q=0.8",
    }

    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True, headers=base_headers) as client:
        # Крок 1: відкриваємо сторінку графіків для отримання cookies
        main_resp = await client.get(MAIN_PAGE_URL)
        main_html = main_resp.text

        # Перевіряємо, чи таблиця не завантажилася одразу в HTML сторінки
        if "light_1" in main_html or "light_2" in main_html:
            return {"html": main_html, "source": "main_page", "main_html": main_html}

        # Крок 2: робимо AJAX POST-запит із отриманими cookies
        ajax_headers = {
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
            "Origin": "https://www.poe.pl.ua",
            "Referer": MAIN_PAGE_URL,
        }

        # Пробуємо стандартні варіанти payload, які використовує poe.pl.ua
        for payload in [
            {"date_in": date_str},
            {"seldate": f'{{"date_in":"{date_str}"}}'},
        ]:
            post_resp = await client.post(POE_URL, data=payload, headers=ajax_headers)
            if len(post_resp.text.strip()) > 0:
                return {"html": post_resp.text, "source": f"post:{payload}", "main_html": main_html}

        return {"html": "", "source": "empty_post", "main_html": main_html}


async def fetch_schedule_from_poe(queue: str) -> Dict[str, Any]:
    now_kyiv = datetime.now(KYIV_TZ)
    res = await raw_poe_request()
    slots = parse_poe_html(res["html"], queue)

    return {
        "queue": queue,
        "date": now_kyiv.strftime("%Y-%m-%d"),
        "date_display": now_kyiv.strftime("%d-%m-%Y"),
        "updated_at": now_kyiv.isoformat(),
        "slots": slots,
        "source": res["source"],
    }


@app.get("/api/debug")
async def debug_poe():
    res = await raw_poe_request()
    html = res["html"]
    main_html = res["main_html"]

    # Шукаємо в main_html згадки про php/gpv/ajax
    snippets = re.findall(r".{0,120}(?:gpv|newgpv|customs|ajax|date_in|seldate).{0,120}", main_html, re.IGNORECASE)

    soup = BeautifulSoup(html, "html.parser")
    summary = []
    for idx, tr in enumerate(soup.find_all("tr")):
        cells = tr.find_all(["td", "th"])
        summary.append({
            "row": idx,
            "total_cells": len(cells),
            "first_4_cells": [c.get_text(strip=True) for c in cells[:4]],
            "sample_classes": [c.get("class") for c in cells[-5:]],
        })

    return JSONResponse({
        "source": res["source"],
        "post_html_length": len(html),
        "main_html_length": len(main_html),
        "ajax_hints_in_main_page": snippets[:15],
        "rows": summary,
    })


@app.get("/api/schedule")
async def get_schedule(
    queue: str = Query(default="1.1", pattern=r"^\d+\.\d+$"),
    force: bool = Query(default=False),
):
    now_ts = time.time()
    cached = _cache.get(queue)

    if not force and cached and (now_ts - cached["timestamp"] < CACHE_TTL_SECONDS):
        return {**cached["data"], "cached": True}

    try:
        data = await fetch_schedule_from_poe(queue)
        _cache[queue] = {"timestamp": now_ts, "data": data}
        return {**data, "cached": False}
    except Exception as exc:
        today_str = datetime.now(KYIV_TZ).strftime("%Y-%m-%d")
        if cached and cached["data"].get("date") == today_str:
            return {
                **cached["data"],
                "cached": True,
                "warning": f"Використано кеш: {exc}",
            }
        raise HTTPException(status_code=502, detail=f"Помилка отримання даних з poe.pl.ua: {exc}")


@app.get("/health")
async def health():
    return {"status": "ok"}


BASE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
STATIC_DIR = os.path.join(BASE_DIR, "static")

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))