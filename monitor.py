import asyncio
import json
import os
import re
from datetime import date, datetime
from pathlib import Path

import requests
from dateutil.relativedelta import relativedelta
from playwright.async_api import async_playwright, Page, Frame

URL = "https://www.uro-logisch.de/marl/onlineterminvereinbarung"
EARLY_CUTOFF = date(2026, 8, 1)
STATE_FILE = Path("state.json")

DATE_RE = re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b")
TIME_RE = re.compile(r"\b([01]?\d|2[0-3]):[0-5]\d\b")
WEEK_RE = re.compile(r"^\s*Woche\s+\d+\s*$", re.IGNORECASE)


def send_telegram(text: str) -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    chunks = []
    while len(text) > 3900:
        split_at = text.rfind("\n", 0, 3900)
        if split_at <= 0:
            split_at = 3900
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip()
    if text:
        chunks.append(text)

    for chunk in chunks:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": chunk, "disable_web_page_preview": False},
            timeout=30,
        )
        response.raise_for_status()


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"initialized": False, "appointments": {}}
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return {
            "initialized": bool(data.get("initialized", False)),
            "appointments": data.get("appointments", {}),
        }
    except Exception:
        return {"initialized": False, "appointments": {}}


def save_state(appointments: dict[str, list[str]]) -> None:
    STATE_FILE.write_text(
        json.dumps(
            {
                "initialized": True,
                "appointments": appointments,
                "checked_at": datetime.now().isoformat(timespec="seconds"),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def extract_appointments(text: str) -> dict[date, set[str]]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    found: dict[date, set[str]] = {}
    current_date: date | None = None
    for line in lines:
        date_match = DATE_RE.search(line)
        if date_match:
            day, month, year = map(int, date_match.groups())
            try:
                current_date = date(year, month, day)
                found.setdefault(current_date, set())
            except ValueError:
                current_date = None
            continue
        if current_date:
            for time_match in TIME_RE.finditer(line):
                found[current_date].add(time_match.group(0))
    return {d: times for d, times in found.items() if times}


def merge_appointments(target: dict[date, set[str]], source: dict[date, set[str]]) -> None:
    for day, times in source.items():
        target.setdefault(day, set()).update(times)


async def frame_text(frame: Frame) -> str:
    try:
        return await frame.locator("body").inner_text(timeout=15000)
    except Exception:
        return ""


async def collect_all_visible_appointments(page: Page) -> dict[date, set[str]]:
    combined: dict[date, set[str]] = {}
    for frame in page.frames:
        merge_appointments(combined, extract_appointments(await frame_text(frame)))
    return combined


async def find_calendar_frame(page: Page) -> Frame:
    for frame in page.frames:
        text = await frame_text(frame)
        if "Verfügbarkeit" in text or ("Woche" in text and DATE_RE.search(text)):
            return frame
    return page.main_frame


async def click_next_week(frame: Frame) -> bool:
    candidates = frame.locator("button, a")
    count = await candidates.count()
    matched = []
    for i in range(count):
        item = candidates.nth(i)
        try:
            text = (await item.inner_text(timeout=2000)).strip()
            if WEEK_RE.match(text):
                box = await item.bounding_box()
                if box:
                    matched.append((box["x"], i))
        except Exception:
            continue
    if not matched:
        return False
    matched.sort(key=lambda x: x[0])
    _, index = matched[-1]
    try:
        await candidates.nth(index).click(timeout=10000)
        await frame.page.wait_for_timeout(2500)
        return True
    except Exception:
        return False


def select_rolling_month(appointments: dict[date, set[str]]) -> dict[date, set[str]]:
    if not appointments:
        return {}
    first_day = min(appointments)
    end_day = first_day + relativedelta(months=1)
    return {day: times for day, times in appointments.items() if first_day <= day <= end_day}


def normalize(appointments: dict[date, set[str]]) -> dict[str, list[str]]:
    return {day.isoformat(): sorted(times) for day, times in sorted(appointments.items())}


def denormalize(data: dict[str, list[str]]) -> dict[date, set[str]]:
    result: dict[date, set[str]] = {}
    for day_str, times in data.items():
        try:
            result[date.fromisoformat(day_str)] = set(times)
        except ValueError:
            continue
    return result


def format_summary(appointments: dict[date, set[str]]) -> str:
    if not appointments:
        return "Свободных терминов сейчас не найдено."
    first_day = min(appointments)
    end_day = first_day + relativedelta(months=1)
    lines = [
        "📅 Актуальные свободные термины",
        f"Период: {first_day.strftime('%d.%m.%Y')} – {end_day.strftime('%d.%m.%Y')}",
        "",
    ]
    for day in sorted(appointments):
        lines.append(f"• {day.strftime('%d.%m.%Y')}: {', '.join(sorted(appointments[day]))}")
    lines.extend(["", URL])
    return "\n".join(lines)


async def main() -> None:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(locale="de-DE", viewport={"width": 1440, "height": 2200})
        await page.goto(URL, wait_until="domcontentloaded", timeout=90000)
        await page.wait_for_timeout(10000)

        calendar_frame = await find_calendar_frame(page)
        all_found: dict[date, set[str]] = {}

        for _ in range(20):
            merge_appointments(all_found, await collect_all_visible_appointments(page))
            if all_found:
                first_day = min(all_found)
                target_end = first_day + relativedelta(months=1)
                if max(all_found) >= target_end:
                    break
            if not await click_next_week(calendar_frame):
                break

        await page.screenshot(path="last_check.png", full_page=True)
        await browser.close()

    current = select_rolling_month(all_found)
    current_normalized = normalize(current)
    state = load_state()
    previous = denormalize(state["appointments"])
    initialized = state["initialized"]

    current_days = set(current)
    previous_days = set(previous)
    gone_days = sorted(previous_days - current_days)
    new_days = sorted(current_days - previous_days)

    removed_times = {
        day: sorted(previous[day] - current.get(day, set()))
        for day in sorted(previous_days & current_days)
        if previous[day] - current.get(day, set())
    }
    added_times = {
        day: sorted(current[day] - previous.get(day, set()))
        for day in sorted(previous_days & current_days)
        if current[day] - previous.get(day, set())
    }

    changed = current_normalized != state["appointments"]

    if not initialized:
        send_telegram("✅ Монитор терминов запущен.\n\n" + format_summary(current))
    elif changed:
        notices = []
        for day in gone_days:
            notices.append(f"❌ День ушёл: {day.strftime('%d.%m.%Y')} — свободных терминов на этот день больше нет.")
        for day, times in removed_times.items():
            notices.append(f"⏳ Термин исчез: {day.strftime('%d.%m.%Y')} — {', '.join(times)}")
        for day in new_days:
            notices.append(f"🆕 Появился новый день: {day.strftime('%d.%m.%Y')} — {', '.join(sorted(current[day]))}")
        for day, times in added_times.items():
            notices.append(f"🆕 Появился термин: {day.strftime('%d.%m.%Y')} — {', '.join(times)}")
        message = "\n".join(notices)
        if message:
            message += "\n\n"
        message += format_summary(current)
        send_telegram(message)

    early_now = {day: times for day, times in current.items() if day < EARLY_CUTOFF}
    early_before = {day: times for day, times in previous.items() if day < EARLY_CUTOFF}
    new_early = []
    for day, times in early_now.items():
        for time_value in sorted(times - early_before.get(day, set())):
            new_early.append((day, time_value))

    if initialized and new_early:
        lines = ["🚨 СРОЧНО: появился термин раньше 01.08.2026!", ""]
        lines.extend(f"• {day.strftime('%d.%m.%Y')} в {time_value}" for day, time_value in new_early)
        lines.extend(["", URL])
        send_telegram("\n".join(lines))

    save_state(current_normalized)
    print("Актуальные термины:", current_normalized)
    print("Исчезнувшие дни:", [d.isoformat() for d in gone_days])


if __name__ == "__main__":
    asyncio.run(main())
