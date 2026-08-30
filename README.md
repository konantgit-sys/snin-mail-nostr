# SNIN Mail — веб-клиент (cryter-mail-web)

Децентрализованная почта поверх Nostr: письма — это NIP-59 gift-wrap события,
хранение — ваши ключи, вложения — Blossom (NIP-96). Клиент: FastAPI-бэкенд
+ esbuild-бандл фронта, дизайн в стиле SNIN Network.

## Возможности

- **NIP-59** — письма шифруются на ключах отправителя/получателя (gift wrap)
- **Мульти-ящик** — 24+ аккаунта в одном клиенте, переключение в шапке
- **Вложения Blossom** — чанковая загрузка, превью, прогресс (NIP-96)
- **IMAP-мост** — импорт/чтение внешних ящиков
- **Черновики** — автосохранение при закрытии композера (`drafts`)
- **Архив** — папка архивных писем (`archived`)
- **Очередь доставки** — мост → очередь → воркеры; письмо переживает рестарт
- **Премиум-дизайн** — aurora-фон, glass-панели, неон #00e0f0, mobile-first

## Архитектура

```
Браузер ──> :8123 FastAPI (uvicorn, 2 воркера)
              ├─ routers/mail.py     — API: login, mails, send, drafts, archive
              ├─ routers/blossom.py  — NIP-96: загрузка вложений, чанки
              ├─ routers/imap.py     — IMAP-мост
              ├─ mailapp/queue.py    — очередь событий (SQLite)
              ├─ mailapp/worker.py   — расшифровка NIP-59 вне WS-периметра
              └─ mailapp/bridge.py   — подписчик релеев (отдельный процесс)
Статика: static/index.html + app.<hash>.js/css (esbuild, long-cache)
```

Три процесса (start.sh): **bridge** (подписка, без ключей) → **worker** (расшифровка)
→ **uvicorn** (API). Ключи — только в воркере, из БД `mail_keys` (зашифрованы master.key).

## Быстрый старт

```bash
# 1. Конфиг (пример → боевой)
cp config.example.json config.json   # заполнить nsec/pubkey/relays/db

# 2. Бэкенд
python3 -m uvicorn app:app --port 8123

# 3. Фронт (сборка бандла после правок static/js/*)
python3 build.py

# 4. Тесты (самодостаточные: config.json генерируется автоматически)
python3 -m pytest tests/ -q          # 93 passed
```

**Самодостаточность тестов:** в свежем клоне без `config.json` conftest.py сам
создаёт тестовый конфиг (случайная парная пара nsec/pubkey, локальная БД,
relays пустые). Боевой config.json не перезаписывается. Для изоляции тесты
monkeypatch'ят cfg.DB/сессии на временные файлы.

## CLI

```bash
# health / статус
python3 scripts/mail_cli.py health
python3 scripts/mail_cli.py status

# письма
python3 scripts/mail_cli.py list                      # входящие
python3 scripts/mail_cli.py list --folder archive     # архив
python3 scripts/mail_cli.py list --folder outbox      # исходящие
python3 scripts/mail_cli.py read 42                   # деталь + тело

# отправка (админ-пароль из config.json или MAIL_PASSWORD)
python3 scripts/mail_cli.py send --to npub1…@snin-mail.v2.site \
    --subject "Привет" --body "Текст" [--attach file.png]

# черновик (создать/обновить/удалить)
python3 scripts/mail_cli.py draft --subject "Черновик" --body "…"
python3 scripts/mail_cli.py draft --id 3 --delete

# архив
python3 scripts/mail_cli.py archive 42                # в архив
python3 scripts/mail_cli.py archive 42 --unarchive    # из архива
```

Параметры: `--url http://localhost:8123`, `--password` (или env `MAIL_PASSWORD`),
`--token` (env `MAIL_TOKEN`, переиспользуется кэш в `~/.cache/mail_cli_token`).

## API (основное)

| Метод | Путь | Назначение |
|---|---|---|
| POST | `/api/login` | вход (nsec или пароль) → token |
| GET | `/api/mails?folder=` | список (inbox/archive), кэш 5с |
| GET | `/api/mails/{id}` | деталь + прочитано |
| POST | `/api/mails/{id}/read` | прочитано/непрочитано |
| POST | `/api/mails/{id}/archive` | `{"archived": true\|false}` |
| DELETE | `/api/mails/{id}` | удалить |
| POST | `/api/send` | отправить |
| POST | `/api/drafts` | сохранить черновик |
| GET | `/api/outbox` | исходящие |
| POST | `/api/blossom/upload` | вложение (NIP-96) |
| GET | `/api/health` | метрики: RAM, БД, очередь |

Авторизация: `Authorization: Bearer <token>` (cookie игнорируются — прокси кеширует Set-Cookie).

## Деплой (v2.site)

- `start.sh` — flock + pkill-якорь: ровно 1 мост, 1 uvicorn, 1 воркер
- `logrotate.conf` — ротация backend/bridge/imap логов (daily, 7)
- `scripts/backup_mail.sh` — бэкап inbox.db + config (KEEP=14)
- `scripts/mail_health_monitor.py` — алерты в Octopus (RAM≥93%, диск≥85%, очередь)
- `scripts/cleanup_caches.py` — чистка uploads/tmp/media (ежедневно)

## Структура

```
app.py                     — точка входа uvicorn
build.py                   — esbuild-сборка фронта (fingerprint-бандлы)
mailapp/                   — бэкенд: auth, bridge, config, db, queue, worker, imap_store
mailapp/routers/           — API: mail, blossom (NIP-96), imap
static/js/                 — исходники фронта: core, api, inbox, detail, composer, main
static/templates/index.src.html — разметка (сборка → static/index.html)
tests/                     — 93 теста: API, Blossom, очередь, IMAP, черновики/архив
scripts/                   — CLI, бэкапы, монитор, чистка кэшей
docs/REFACTORING_SPEC.md   — спека фаз 0–5
```

## Правила разработки

1. Тесты обязательны после каждой правки: `pytest` (93, зелёные)
2. Бандлы — артефакты: после правок `static/js/*` пересобрать `build.py`
3. Боевые секреты (config.json, keys/, .sessions.json) — в `.gitignore`
4. Деплой и git-зеркало держим в одном состоянии (diff = 0)
