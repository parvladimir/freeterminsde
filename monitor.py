import asyncio
import html
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
WEEK_RE = re.compile(r"^\s*Woche\s+\d+\s*$", re.I)
WEEKDAYS_RU = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]


def send_telegram(text: str, silent: bool = False) -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    while text:
        chunk = text[:3900]
        if len(text) > 3900 and "\n" in chunk:
            chunk = chunk[:chunk.rfind("\n")]
        text = text[len(chunk):].lstrip()
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": chunk,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
                "disable_notification": silent,
            },
            timeout=30,
        )
        r.raise_for_status()


def load_state() -> dict:
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return {"initialized": bool(data.get("initialized")), "appointments": data.get("appointments", {})}
    except Exception:
        return {"initialized": False, "appointments": {}}


def save_state(appts: dict[str, list[str]]) -> None:
    STATE_FILE.write_text(json.dumps({
        "initialized": True,
        "appointments": appts,
        "checked_at": datetime.now().isoformat(timespec="seconds"),
    }, ensure_ascii=False, indent=2), encoding="utf-8")


def extract(text: str) -> dict[date, set[str]]:
    result: dict[date, set[str]] = {}
    current = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = DATE_RE.search(line)
        if m:
            try:
                current = date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
                result.setdefault(current, set())
            except ValueError:
                current = None
            continue
        if current:
            for t in TIME_RE.finditer(line):
                result[current].add(t.group(0))
    return {d: times for d, times in result.items() if times}


def merge(dst, src):
    for d, times in src.items():
        dst.setdefault(d, set()).update(times)


async def body_text(frame: Frame) -> str:
    try:
        return await frame.locator("body").inner_text(timeout=15000)
    except Exception:
        return ""


async def calendar_frame(page: Page) -> Frame:
    for frame in page.frames:
        text = await body_text(frame)
        if "Verfügbarkeit" in text or "verfügbaren Termine" in text or ("Woche" in text and DATE_RE.search(text)):
            return frame
    return page.main_frame


async def all_frame_dates(page: Page) -> dict[date, set[str]]:
    out = {}
    for frame in page.frames:
        merge(out, extract(await body_text(frame)))
    return out


async def get_type_options(page: Page) -> list[tuple[str, str]]:
    frame = await calendar_frame(page)
    selects = frame.locator("select")
    for i in range(await selects.count()):
        select = selects.nth(i)
        options = select.locator("option")
        found = []
        for j in range(await options.count()):
            opt = options.nth(j)
            value = await opt.get_attribute("value")
            label = (await opt.inner_text()).strip()
            if value is not None and label:
                found.append((value, label))
        if len(found) > 1:
            return found
    return []


async def select_type(page: Page, value: str) -> bool:
    frame = await calendar_frame(page)
    selects = frame.locator("select")
    for i in range(await selects.count()):
        select = selects.nth(i)
        try:
            opts = select.locator("option")
            values = [await opts.nth(j).get_attribute("value") for j in range(await opts.count())]
            if value in values:
                await select.select_option(value=value)
                await page.wait_for_timeout(5000)
                return True
        except Exception:
            pass
    return False


async def click_available_list(page: Page) -> None:
    frame = await calendar_frame(page)
    for pattern in [r"Die nächsten\s+\d+\s+verfügbaren Termine", r"verfügbaren Termine"]:
        loc = frame.get_by_text(re.compile(pattern, re.I))
        try:
            if await loc.count() and await loc.first.is_visible():
                await loc.first.click(timeout=5000)
                await page.wait_for_timeout(3500)
                return
        except Exception:
            pass


async def click_next_week(page: Page) -> bool:
    frame = await calendar_frame(page)
    loc = frame.locator("button, a")
    choices = []
    for i in range(await loc.count()):
        try:
            txt = (await loc.nth(i).inner_text(timeout=1000)).strip()
            if WEEK_RE.match(txt):
                box = await loc.nth(i).bounding_box()
                if box:
                    choices.append((box["x"], i))
        except Exception:
            pass
    if not choices:
        return False
    _, idx = sorted(choices)[-1]
    try:
        await loc.nth(idx).click(timeout=7000)
        await page.wait_for_timeout(2500)
        return True
    except Exception:
        return False


async def collect_one_type(page: Page) -> dict[date, set[str]]:
    found = await all_frame_dates(page)
    await click_available_list(page)
    merge(found, await all_frame_dates(page))
    if found:
        return found
    # Fallback to the weekly calendar used by the previously working version.
    for _ in range(12):
        merge(found, await all_frame_dates(page))
        if not await click_next_week(page):
            break
    return found


def normalize(appts):
    return {d.isoformat(): sorted(times) for d, times in sorted(appts.items())}


def denormalize(data):
    out = {}
    for key, times in data.items():
        try:
            out[date.fromisoformat(key)] = set(times)
        except ValueError:
            pass
    return out


def rolling_month(appts):
    if not appts:
        return {}
    first = min(appts)
    end = first + relativedelta(months=1)
    return {d: t for d, t in appts.items() if first <= d <= end}


def title(day):
    return f"{WEEKDAYS_RU[day.weekday()]}, {day.strftime('%d.%m.%Y')}"


def plural(n):
    if n % 10 == 1 and n % 100 != 11: return "термин"
    if n % 10 in (2,3,4) and n % 100 not in (12,13,14): return "термина"
    return "терминов"


def early_message(early):
    lines = ["🚨🚨🚨", "<b>НАЙДЕН РАННИЙ ТЕРМИН!</b>", "━━━━━━━━━━━━━━━━━━", ""]
    for d in sorted(early):
        lines += [f"<b>📅 {title(d)}</b>", f"🕒 <code>{'   '.join(sorted(early[d]))}</code>", ""]
    lines += ["<b>Открывай сайт и бронируй как можно быстрее.</b>", "", f'<a href="{URL}">👉 ЗАПИСАТЬСЯ СЕЙЧАС</a>', "", "🚨🚨🚨"]
    return "\n".join(lines)


def summary(appts):
    first, end = min(appts), min(appts) + relativedelta(months=1)
    lines = ["<b>🏥 UROLOGIE MARL</b>", "━━━━━━━━━━━━━━━━━━", "<b>📆 Актуальные свободные дни</b>", f"{first:%d.%m.%Y} — {end:%d.%m.%Y}", ""]
    for d in sorted(appts):
        n = len(appts[d]); lines.append(f"🟢 <b>{d:%d.%m.%Y}</b> · {n} {plural(n)}")
    lines += ["", f'<a href="{URL}">🔗 Открыть запись</a>']
    return "\n".join(lines)


def changes_message(prev, cur):
    prev_days, cur_days = set(prev), set(cur)
    gone, new = sorted(prev_days-cur_days), sorted(cur_days-prev_days)
    lines = ["<b>🔔 ИЗМЕНЕНИЯ В КАЛЕНДАРЕ</b>", "━━━━━━━━━━━━━━━━━━"]
    for d in new:
        lines += ["", "<b>🆕 Новый свободный день</b>", f"📅 <b>{title(d)}</b>", f"🕒 <code>{'   '.join(sorted(cur[d]))}</code>"]
    for d in gone:
        lines += ["", "<b>❌ День полностью ушёл</b>", f"📅 <b>{title(d)}</b>", "Свободных терминов больше нет."]
    for d in sorted(prev_days & cur_days):
        add, rem = sorted(cur[d]-prev[d]), sorted(prev[d]-cur[d])
        if add or rem:
            lines += ["", f"<b>🔄 {title(d)}</b>"]
            if add: lines.append(f"➕ <code>{'   '.join(add)}</code>")
            if rem: lines.append(f"➖ <code>{'   '.join(rem)}</code>")
    lines += ["", f'<a href="{URL}">🔗 Открыть запись</a>']
    return "\n".join(lines)


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(locale="de-DE", viewport={"width": 1440, "height": 2400})
        await page.goto(URL, wait_until="domcontentloaded", timeout=90000)
        await page.wait_for_timeout(12000)

        all_found = {}
        options = await get_type_options(page)
        if not options:
            merge(all_found, await collect_one_type(page))
        else:
            for value, label in options:
                print("Checking appointment type:", label)
                if await select_type(page, value):
                    merge(all_found, await collect_one_type(page))

        await page.screenshot(path="last_check.png", full_page=True)
        await browser.close()

    # Never overwrite state and never spam Telegram if parsing failed.
    if not all_found:
        print("ERROR: calendar returned no appointments; state preserved")
        raise RuntimeError("Calendar could not be parsed")

    state = load_state()
    previous = denormalize(state["appointments"])
    current = rolling_month(all_found)

    # If most previous days suddenly vanish, treat this as an incomplete read.
    gone = set(previous) - set(current)
    if previous and len(gone) >= max(3, int(len(previous) * 0.7)) and len(current) < 2:
        print("ERROR: suspicious mass disappearance; state preserved")
        raise RuntimeError("Suspicious incomplete calendar read")

    early_now = {d: t for d, t in all_found.items() if d < EARLY_CUTOFF}
    early_prev = {d: t for d, t in previous.items() if d < EARLY_CUTOFF}
    new_early = {d: t - early_prev.get(d, set()) for d, t in early_now.items() if t - early_prev.get(d, set())}

    if new_early:
        send_telegram(early_message(new_early), silent=False)

    if not state["initialized"]:
        send_telegram("<b>✅ Монитор запущен</b>\n\n" + summary(current))
    elif normalize(current) != state["appointments"]:
        send_telegram(changes_message(previous, current), silent=True)

    save_state(normalize(current))
    print("All appointments:", normalize(all_found))
    print("Early appointments:", normalize(early_now))


if __name__ == "__main__":
    asyncio.run(main())
