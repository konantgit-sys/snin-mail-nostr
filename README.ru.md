# SNIN Mail — децентрализованная почта на Nostr

> English version: [README.md](README.md)

Собственная почта на Nostr: письма — это NIP-59 gift-wrap события, шифрование —
NIP-44 (v2), вложения — Blossom (NIP-96). Ваши ключи, ваша почта, ваши релеи —
без серверов и слежки.

**Единый репозиторий**: веб-клиент + протокольное ядро (NIP-44/59) + IMAP-мост
+ Docker-деплой в одном месте. Наследник заархивированного `nostr-mail-bridge`
(авг 2026): ядро протокола влито в клиент, проект унифицирован.

## Возможности

- **NIP-59 gift wrap** — отправитель скрыт эфемерным ключом, контент зашифрован дважды
- **NIP-44 (v2)** — ECDH + HKDF + ChaCha20-Poly1305, проверено тест-векторами (`docs/nip44.vectors.json`)
- **NIP-96 Blossom** — чанковая загрузка вложений, превью, прогресс
- **Мульти-ящик** — много аккаунтов в одном клиенте
- **IMAP-мост** — импорт/чтение внешних ящиков (`docs/GUIDE-imap.md`)
- **Очередь доставки** — подписчик → SQLite-очередь → воркеры; письмо переживает рестарт
- **Черновики** — автосохранение при закрытии композера
- **Архив** — папка архивных писем
- **Docker** — деплой одной командой (`docker compose up -d`)
- **CLI** — отправка/чтение/черновики/архив из терминала (`scripts/mail_cli.py`)
- **Мониторинг** — алерты RAM/диск/очередь в Telegram (`scripts/mail_health_monitor.py`)

## Архитектура

```
Браузер ──> FastAPI (uvicorn, 2 воркера) :8123
              ├─ mailapp/routers/mail.py     — login, письма, send, drafts, archive
              ├─ mailapp/routers/blossom.py  — NIP-96 вложения
              ├─ mailapp/routers/imap.py     — IMAP-мост
              ├─ mailapp/queue.py            — очередь событий (SQLite)
              ├─ mailapp/worker.py           — расшифровка NIP-59 вне WS-периметра
              └─ mailapp/bridge.py           — подписчик релеев (без ключей)

mailbridge/                — ядро протокола: nip44, nip59, mail_message, blossom, imap_bridge
static/                    — esbuild-бандл (fingerprint, long-cache)
```

Три процесса (`start.sh`): **bridge** (подписка, без ключей) → **worker**
(расшифровка) → **uvicorn** (API). Ключи — только в воркере, зашифрованы
master-ключом.

## Быстрый старт

```bash
cp config.example.json config.json      # заполнить nsec/pubkey/relays/db
pip3 install -r requirements.txt
python3 -m uvicorn app:app --port 8123  # запуск
python3 build.py                        # пересборка фронта после static/js/*
python3 -m pytest tests/ -q             # 157 тестов, зелёные
```

### Docker

```bash
cd docker && docker compose up -d
```

## CLI

```bash
python3 scripts/mail_cli.py health | status
python3 scripts/mail_cli.py list                     # входящие
python3 scripts/mail_cli.py list --folder archive    # архив
python3 scripts/mail_cli.py read 42
python3 scripts/mail_cli.py send --to npub1…@snin-mail.v2.site \
    --subject "Привет" --body "Текст" [--attach file.png]
python3 scripts/mail_cli.py draft --subject "Черновик" --body "…"
python3 scripts/mail_cli.py archive 42 [--unarchive]
```

## API (основное)

| Метод | Путь | Назначение |
|---|---|---|
| POST | `/api/login` | вход (nsec/пароль) → token |
| GET | `/api/mails?folder=` | список (inbox/archive), кэш 5с |
| GET | `/api/mails/{id}` | деталь + прочитано |
| POST | `/api/mails/{id}/read` | прочитано/непрочитано |
| POST | `/api/mails/{id}/archive` | `{"archived": true\|false}` |
| DELETE | `/api/mails/{id}` | удалить |
| POST | `/api/send` | отправить |
| POST | `/api/drafts` | черновик |
| GET | `/api/outbox` | исходящие |
| POST | `/api/blossom/upload` | вложение (NIP-96) |
| GET | `/api/health` | RAM, БД, очередь |

Авторизация: `Authorization: Bearer <token>`.

## Документация

- `docs/SPEC.md` — полная спека проекта
- `docs/NIP-44.md`, `docs/NIP-59.md` — заметки по протоколу + тест-векторы
- `docs/GUIDE-nostrmail.md` — как устроены адреса SNIN Mail
- `docs/GUIDE-imap.md` — настройка IMAP-моста
- `docs/GUIDE-friends.md` — почта «друг-другу»
- `docs/DEPLOY.md` — боевой деплой
- `docs/REFACTORING_SPEC.md` — история фаз 0–5

## Правила разработки

1. Тесты обязательны после каждой правки: `pytest` (157, зелёные)
2. Бандлы — артефакты: после `static/js/*` пересобрать `build.py`
3. Секреты (`config.json`, `keys/`, `.sessions.json`) — в `.gitignore`
4. Деплой и git-зеркало держим в одном состоянии (diff = 0)
