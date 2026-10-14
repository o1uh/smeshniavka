# DEPLOY.md — деплой «Смешнявки» (для хостера)

Результат задачи TB-10. Разработка уже завершена автономно: репо приезжает с зелёными unit-тестами, `Dockerfile`, `docker-compose.yml`, `ops/bot.service`, миграциями и `src/tools/*`. Ниже — единственный порядок запуска с реальным токеном, ключами и чатом.

> Приватность: хостер имеет доступ к БД с перепиской и досье (`data/bot.db`, `reports/`). Предупредите участников чата об этом до запуска.

## 0. Требования

- Вариант A: Docker + compose.
- Вариант B: Python 3.11+ и ffmpeg (`ffmpeg -version` обязан работать; бот проверяет ffmpeg при `--dry-run` и на старте).
- Доступ в интернет к Telegram Bot API и Gemini API.

## 1. Создание бота

1. В BotFather: `/newbot` → сохранить токен в `TG_TOKEN`.
2. В BotFather: `/setprivacy → Disable` (иначе бот не видит сообщения чата).
3. Добавьте бота в целевой групповой чат админом (нужно для чтения реакций).
4. В BotFather: `/setjoingroups → Disable` (после добавления в чат, чтобы бота не тащили по посторонним чатам).

## 2. Узнать ALLOWED_CHAT_ID и OWNER_IDS

```bash
export TG_TOKEN=...
python -m src.tools.discover
```

- Временный режим: 120 секунд слушает входящие апдейты, печатает `chat_id` и `user_id` в stdout, ничего не хранит в БД и не отвечает, затем выходит.
- Напишите что-нибудь в целевом чате, сверьте напечатанный `chat_id` с ожидаемым чатом.
- `OWNER_IDS` — `user_id` владельцев из вывода (через запятую).

## 3. Заполнение `.env` и конфига

`.env` (права 600, не коммитить):

```env
TG_TOKEN=...
GEMINI_KEY_1=...
GEMINI_KEY_2=...
# ... столько, сколько ключей; имена соответствуют keys.yaml:key_env
```

`.env.example` содержит только имена без значений.

`config/bot.yaml`:

- `ALLOWED_CHAT_ID` — id целевого чата из шага 2.
- `OWNER_IDS` — список id владельцев (env или конфиг — как принято в репо; канон — env).
- `MODEL_ID` — обязателен, без дефолта (например актуальный flash из AI Studio; при старте проверяется через models API, при ошибке — понятное сообщение).
- `TZ` — дефолт `Asia/Novosibirsk`; `NIGHT_START/NIGHT_END` считаются в TZ.
- `SAFETY=all OFF` — уже дефолт в репо.

`config/keys.yaml` — только ссылки вида `key_env: GEMINI_KEY_1` + лимиты, без секретов. Число ключей произвольное.

## 4. Первый запуск в SHADOW_MODE=true

```bash
# Docker:
docker compose up -d --build
# или systemd:
sudo cp ops/bot.service /etc/systemd/system/smeshnyavka.service
sudo systemctl enable --now smeshnyavka
```

- Первый запуск всегда с `SHADOW_MODE=true`: бот ничего не отправляет, пишет решения и «ответил бы: …» в `reports/shadow.log` (права 600).
- Смотрите лог: `tail -f reports/shadow.log`. Проверьте частоту проактива, отсутствие отправок, отсутствие ошибок миграций.
- Проверка здоровья: `python -m src.tools.healthcheck` (смотрит mtime `data/heartbeat`).

## 5. Опциональный импорт истории (в shadow)

```bash
python -m src.tools.import_history parse
python -m src.tools.import_history summary+dossier
```

- Стадии: `parse` (парс экспорта Telegram Desktop JSON в `messages`, идемпотентно по `message_id`), затем `summary+dossier` (map-reduce саммари + досье по окнам 400, стоп после `IMPORT_MAX_CALLS=300`).
- Лимит `IMPORT_MAX_DAYS=30` (валидируется как ≤ `MSG_RETENTION_DAYS`, иначе ошибка конфига).
- Запускать только в shadow, до перевода в live.

## 6. Перевод в live и рестарт

1. Поставьте `SHADOW_MODE=false` в `config/bot.yaml` (или env — как принято в репо).
2. Рестарт: `docker compose restart` или `sudo systemctl restart smeshnyavka`.
3. Приёмка: отправьте 3 пинга (reply на бота, @mention, вопрос с алиасом) — бот отвечает <10с, в чат не уходит «uN».
4. Проверьте `/mute 10м` от OWNER и `/alive`.

## 7. Бэкап и восстановление

- Бэкап SQLite — внутри приложения (asyncio-задача, `VACUUM INTO`, раз в сутки, хранить 7 шт. в `backups/`). Cron не нужен.
- Живой банк стиля (`prompts/style_examples.yaml`, `prompts/style_examples.candidates.yaml`) деплой не затирает (исключения в `deploy.sh`), бот дописывает их сам; при желании правится вручную.
- Восстановление: остановить бота, затем `python -m src.tools.restore <file>` (путь из `backups/`), запустить снова.
- `data/`, `reports/`, `backups/` — права 700; логи/репорты — 600.

## 8. Обновление

```bash
git pull
docker compose up -d --build
# или: sudo systemctl restart smeshnyavka
```

Миграции (`migrations/NNN_*.sql`, `PRAGMA user_version`) применяются автоматом при старте; перед миграцией — автобэкап `VACUUM INTO`; событие «выполнена миграция» уходит алертом OWNER в ЛС.

## 9. Траблшутинг

| Симптом | Что делать |
|---|---|
| `409 Conflict` / выход 75 | работает второй инстанс. Остановить дубль; бот сам ждёт 30с→5м до `CONFLICT_MAX_WAIT_S=900` и выходит с 75 (systemd/compose перезапустят). Алерт придёт OWNER в ЛС |
| Все ключи в кулдауне | бот отвечает отмазками без API, фон стоит. Подождать до полуночи PT (≈14:00–15:00 в `Asia/Novosibirsk`) или добавить ключ. Алерт при кулдауне дольше `ALERT_KEYS_DOWN_S=600` |
| Ключ 401/403 | проверить `GEMINI_KEY_*` в `.env`, соответствие `key_env`; алерт OWNER |
| Токен TG 401 | проверить `TG_TOKEN` |
| Старт после падения | алерт «нет `data/clean_shutdown`»; смотреть `logs/bot.log` |
| Мало диска | алерт при <`ALERT_DISK_MIN_MB=500`; почистить `logs/`, старые бэкапы |
| Посторонний чат | бот сам делает `leaveChat` + пишет в `foreign_chats`; алерт OWNER. Ничего делать не нужно |
| Миграция group→supergroup | бот сам принимает новый id (оверрайд `data/runtime_state.json`), переносит данные, шлёт алерт. `ALLOWED_CHAT_ID` в конфиге можно обновить позже |
| `can't parse entities` 400 | бот сам повторяет plain text; если повторяется — смотреть `logs/bot.log` |
| Нет `data/heartbeat` | процесс висит — рестарт сервиса, смотреть healthcheck |

Алерты — только в ЛС OWNER (`OWNER_IDS`); остальным ЛС бот не отвечает (кроме `/start` от OWNER — подтверждение, иначе Telegram не даст писать первым). 403 при отправке ЛС — только лог.
