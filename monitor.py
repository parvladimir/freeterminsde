from __future__ import annotations

import html
import json
import os
import re
import time
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Iterable

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

PRACTICE_URL = "https://www.uro-logisch.de/marl/onlineterminvereinbarung"
DOCVISIT_LIST_URL = "https://www.docvisit.de/kalender/marl/list"
KNOWN_TYPE_IDS = {"2866438"}  # Резерв: Termin Herr Dr-medic Kamal
EARLY_CUTOFF = date(2026, 8, 1)
STATE_FILE = Path("state.json")
DEBUG_HTML = Path("last_response.html")

DATE_RE = re.compile(r"\b([0-3]?\d)\.([01]?\d)\.(20\d{2})\b")
TIME_RE = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b")
WEEKDAYS_RU = (
    "Понедельник", "Вторник", "Среда", "Четверг",
    "Пятница", "Суббота", "Воскресенье",
)


def session() -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=4,
        connect=4,
        read=4,
        status=4,
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        respect_retry_after_header=True,
    )
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/136.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "de-DE,de;q=0.9,en;q=0.7",
        "Referer": PRACTICE_URL,
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    })
    return s


def telegram(text: str, *, loud: bool = False) -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "disable_notification": not loud,
        "reply_markup": {
            "inline_keyboard": [[
                {"text": "📅 Открыть запись", "url": PRACTICE_URL}
            ]]
        },
    }
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json=payload,
        timeout=30,
    )
    r.raise_for_status()


def load_state() -> dict:
    default = {
        "initialized": False,
        "appointments": {},
        "failure_count": 0,
        "last_error_notice_at": 0,
    }
    if not STATE_FILE.exists():
        return default
    try:
        saved = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        default.update(saved)
    except Exception:
        pass
    return default


def save_state(state: dict) -> None:
    state["checked_at"] = datetime.now().isoformat(timespec="seconds")
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def discover_types(s: requests.Session) -> dict[str, str]:
    """Находит реальные type ID и названия приёмов прямо на странице DocVisit."""
    found: dict[str, str] = {}
    r = s.get(DOCVISIT_LIST_URL, timeout=35)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    for option in soup.select("select option"):
        value = (option.get("value") or "").strip()
        label = option.get_text(" ", strip=True)
        if value and value not in {"0", "-1"} and re.fullmatch(r"\d+", value):
            found[value] = label or f"Тип {value}"

    for type_id in KNOWN_TYPE_IDS:
        found.setdefault(type_id, f"Тип {type_id}")

    return found


def parse_appointments(raw_html: str) -> dict[date, set[str]]:
    """
    Разбирает список DocVisit без привязки к CSS-классам.
    Каждая дата получает времена до следующей даты.
    """
    soup = BeautifulSoup(raw_html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text("\n", strip=True)

    matches = list(DATE_RE.finditer(text))
    result: dict[date, set[str]] = defaultdict(set)

    for index, match in enumerate(matches):
        try:
            day = date(
                int(match.group(3)),
                int(match.group(2)),
                int(match.group(1)),
            )
        except ValueError:
            continue

        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        block = text[match.end():end]
        for hour, minute in TIME_RE.findall(block):
            result[day].add(f"{int(hour):02d}:{minute}")

    return {day: times for day, times in result.items() if times}


def fetch_snapshot() -> tuple[dict[date, set[str]], dict[str, str], str]:
    s = session()
    types = discover_types(s)
    combined: dict[date, set[str]] = defaultdict(set)
    successful: dict[str, str] = {}
    debug_parts: list[str] = []

    for type_id, label in types.items():
        try:
            r = s.get(
                DOCVISIT_LIST_URL,
                params={"type": type_id, "_": int(time.time())},
                timeout=35,
            )
            r.raise_for_status()
            parsed = parse_appointments(r.text)
            debug_parts.append(
                f"<!-- TYPE {type_id}: {label}; FOUND {sum(map(len, parsed.values()))} -->\n"
                + r.text
            )
            if parsed:
                successful[type_id] = label
                for day, times in parsed.items():
                    combined[day].update(times)
        except requests.RequestException as exc:
            debug_parts.append(f"<!-- TYPE {type_id} ERROR: {exc!r} -->")

    debug_html = "\n\n".join(debug_parts)
    return dict(combined), successful, debug_html


def normalized(snapshot: dict[date, set[str]]) -> dict[str, list[str]]:
    return {
        day.isoformat(): sorted(times)
        for day, times in sorted(snapshot.items())
    }


def restored(data: dict[str, list[str]]) -> dict[date, set[str]]:
    result: dict[date, set[str]] = {}
    for key, values in data.items():
        try:
            result[date.fromisoformat(key)] = set(values)
        except (ValueError, TypeError):
            continue
    return result


def date_name(day: date) -> str:
    return f"{WEEKDAYS_RU[day.weekday()]}, {day.strftime('%d.%m.%Y')}"


def plural(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "термин"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return "термина"
    return "терминов"


def times_grid(times: Iterable[str]) -> str:
    values = sorted(times)
    return "\n".join(
        "   ".join(values[i:i + 4])
        for i in range(0, len(values), 4)
    )


def early_message(early: dict[date, set[str]]) -> str:
    lines = [
        "🚨🚨🚨",
        "<b>НАЙДЕН РАННИЙ ТЕРМИН</b>",
        "━━━━━━━━━━━━━━━━━━",
        "",
    ]
    for day in sorted(early):
        lines.extend([
            f"📅 <b>{html.escape(date_name(day))}</b>",
            f"🕒 <code>{html.escape(times_grid(early[day]))}</code>",
            "",
        ])
    lines.extend([
        "<b>Бронируй как можно быстрее.</b>",
        "🚨🚨🚨",
    ])
    return "\n".join(lines)


def initial_message(snapshot: dict[date, set[str]]) -> str:
    total = sum(len(v) for v in snapshot.values())
    lines = [
        "<b>✅ МОНИТОР ЗАПУЩЕН</b>",
        "━━━━━━━━━━━━━━━━━━",
        f"Свободных дней: <b>{len(snapshot)}</b>",
        f"Свободных терминов: <b>{total}</b>",
        "",
    ]
    for day in sorted(snapshot)[:20]:
        count = len(snapshot[day])
        lines.append(
            f"🟢 <b>{day.strftime('%d.%m.%Y')}</b> · {count} {plural(count)}"
        )
    return "\n".join(lines)


def change_message(
    previous: dict[date, set[str]],
    current: dict[date, set[str]],
) -> str | None:
    old_days, new_days = set(previous), set(current)
    added_days = sorted(new_days - old_days)
    gone_days = sorted(old_days - new_days)

    added_times = {
        day: current[day] - previous.get(day, set())
        for day in sorted(new_days)
        if current[day] - previous.get(day, set())
    }
    removed_times = {
        day: previous[day] - current.get(day, set())
        for day in sorted(old_days)
        if previous[day] - current.get(day, set())
    }

    if not (added_times or removed_times):
        return None

    lines = [
        "<b>🔔 ИЗМЕНЕНИЯ В КАЛЕНДАРЕ</b>",
        "━━━━━━━━━━━━━━━━━━",
    ]

    for day in added_days:
        lines.extend([
            "",
            "<b>🆕 Новый свободный день</b>",
            f"📅 <b>{html.escape(date_name(day))}</b>",
            f"🕒 <code>{html.escape(times_grid(current[day]))}</code>",
        ])

    for day in gone_days:
        lines.extend([
            "",
            "<b>❌ День полностью ушёл</b>",
            f"📅 <b>{html.escape(date_name(day))}</b>",
        ])

    changed_existing = sorted((old_days & new_days))
    for day in changed_existing:
        plus = added_times.get(day, set())
        minus = removed_times.get(day, set())
        if not plus and not minus:
            continue
        lines.extend(["", f"<b>🔄 {html.escape(date_name(day))}</b>"])
        if plus:
            lines.append(f"➕ <code>{html.escape('   '.join(sorted(plus)))}</code>")
        if minus:
            lines.append(f"➖ <code>{html.escape('   '.join(sorted(minus)))}</code>")

    return "\n".join(lines)


def suspicious_drop(previous: dict[date, set[str]], current: dict[date, set[str]]) -> bool:
    if not previous:
        return False
    old_slots = sum(map(len, previous.values()))
    new_slots = sum(map(len, current.values()))
    return new_slots < max(1, int(old_slots * 0.25))


def main() -> None:
    state = load_state()
    previous = restored(state.get("appointments", {}))

    current, successful_types, debug_html = fetch_snapshot()
    DEBUG_HTML.write_text(debug_html, encoding="utf-8")

    # Один повторный запрос защищает от кратковременного неполного ответа.
    if not current or suspicious_drop(previous, current):
        time.sleep(15)
        retry_current, retry_types, retry_debug = fetch_snapshot()
        DEBUG_HTML.write_text(retry_debug, encoding="utf-8")
        if retry_current:
            current, successful_types = retry_current, retry_types

    if not current:
        state["failure_count"] = int(state.get("failure_count", 0)) + 1
        # Не спамим: сообщение при первой ошибке и затем каждую шестую.
        if state["failure_count"] == 1 or state["failure_count"] % 6 == 0:
            telegram(
                "<b>⚠️ Проверка временно не удалась</b>\n\n"
                "Старые данные сохранены. Монитор попробует снова автоматически.",
                loud=False,
            )
        save_state(state)
        print("WARNING: DocVisit returned no parseable appointments; state preserved.")
        return

    if suspicious_drop(previous, current):
        state["failure_count"] = int(state.get("failure_count", 0)) + 1
        save_state(state)
        print("WARNING: suspiciously incomplete snapshot; state preserved.")
        return

    state["failure_count"] = 0

    # Важное: тревога рассчитывается по полному снимку, не по месячному окну.
    early_current = {
        day: times for day, times in current.items() if day < EARLY_CUTOFF
    }
    early_previous = {
        day: times for day, times in previous.items() if day < EARLY_CUTOFF
    }
    new_early = {
        day: times - early_previous.get(day, set())
        for day, times in early_current.items()
        if times - early_previous.get(day, set())
    }

    if new_early:
        telegram(early_message(new_early), loud=True)

    if not state.get("initialized", False):
        telegram(initial_message(current), loud=False)
    else:
        changes = change_message(previous, current)
        if changes:
            telegram(changes, loud=False)

    state.update({
        "initialized": True,
        "appointments": normalized(current),
        "successful_types": successful_types,
    })
    save_state(state)

    print("Successful appointment types:", successful_types)
    print("Appointments:", normalized(current))
    print("Early appointments:", normalized(early_current))


if __name__ == "__main__":
    main()
