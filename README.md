# Urologie Marl Termin-Monitor v6

Надёжная версия без Playwright и Chromium.

Монитор напрямую читает HTML DocVisit, автоматически обнаруживает типы приёма,
проверяет ранние даты, защищается от пустых и неполных ответов и не затирает
правильное состояние при временном сбое сайта.

Для обновления существующего репозитория замените:

- `monitor.py`
- `requirements.txt`
- `.github/workflows/check.yml`
- `state.json`

После замены выполните ручной запуск в GitHub Actions.
