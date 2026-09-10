# Обычный email → SNIN-почта (IMAP-мост)

Документ: 2026-08-28. Направление работает **без собственного домена**.

## Зачем

Получать письма с любых обычных сервисов (банки, подписки, уведомления)
прямо в SNIN-почту: открываешь snin-mail.v2.site — и видишь всё в одном
ящике, в Nostr-контуре.

## Как работает

```
обычный email → IMAP (mail.ru и др.) → imap_bridge.py → kind:1301 (NIP-59)
→ релеи → мост snin-mail → ящик владельца
```

## Настройка (2 шага)

### 1. Создать ящик с IMAP

- mail.ru (бесплатно): настройки → «Пароли для внешних приложений» →
  создать пароль приложения (IMAP). Обычный пароль для входа в веб — не подходит.
- Проверка: `imap.mail.ru:993` SSL.

### 2. Конфиг (секрет, НЕ в git)

`/home/agent/data/.secure/imap_config.json`:

```json
{
  "host": "imap.mail.ru",
  "port": 993,
  "ssl": true,
  "user": "ВАШ_ЛОГИН@mail.ru",
  "app_password": "ПАРОЛЬ_ПРИЛОЖЕНИЯ",
  "target_owner": "cryter",
  "poll_seconds": 120
}
```

`target_owner` — label владельца ящика SNIN (по умолчанию `cryter`;
для агентов — их label из accounts: `director_ai`, `axiom`, …).

## Запуск

```bash
cd /home/agent/data/sites/cryter-mail
PYTHONPATH=/home/agent/data/projects/nostr-mail-bridge/src:/home/agent/data/projects/nostr-mail-bridge/deps \
  python3 -m mailbridge.imap_bridge --once     # один проход (проверка)
PYTHONPATH=... python3 -m mailbridge.imap_bridge  # демон (цикл)
```

Для автозапуска после рестарта пода — добавить команду в
`/home/agent/data/init.sh`.

## Что умеет / что нет

- ✅ Текст (text/plain), кириллица, вложения (base64 в теле письма)
- ✅ Непрочитанные → прочитанные после доставки (сбойные остаются UNSEEN)
- ✅ Маршрутизация в ящик владельца через полный Nostr-контур
- ⏳ Исходящие на обычный email — только после покупки домена (нужен MX/DKIM/SPF)
- ⏳ Вложения через Blossom (NIP-96) для IMAP-писем — отдельная задача

## Тесты

```bash
cd sites/cryter-mail && NO_BRIDGE=1 python3 -m pytest tests/test_imap_bridge.py -v
```

---

## Режим 2 (основной, с 2026-08-28): МУЛЬТИ-ЮЗЕР — каждый сам

Раньше был ОДИН общий конфиг `.secure/imap_config.json` → письма шли в ящик
одного владельца. Теперь **каждый пользователь/агент подключает СВОЙ внешний
IMAP-ящик через веб-клиент** — и письма приходят в ЕГО SNIN-ящик по полному
контуру:

```
внешний IMAP (mail.ru и др.)
  → демон imap_bridge (fetch, каждые 120 с)
  → kind:1301, NIP-59 gift wrap, подпись ключом ВЛАДЕЛЬЦА (p-тег = его pubkey)
  → релеи (3)
  → мост владельца (SharedSubscriber)
  → его inbox (imap_configs → доставка)
```

### Как пользователь подключает

1. Заходит на snin-mail.v2.site → логин (свой npub + пароль ящика).
2. Вкладка **«Входящие (IMAP)»** → форма: хост, порт, SSL, логин, app-пароль.
3. Сохранить. Демон подхватывает конфиг в течение минуты.

### API

- `GET    /api/imap/config` — свой конфиг (пароль маской)
- `PUT    /api/imap/config` — `{host, port, ssl, user, app_password?}` (пустой пароль = не менять)
- `DELETE /api/imap/config` — отключить
- `GET    /api/imap/status` — `{enabled, last_sync, last_error}`

Пароль шифруется AES-256-GCM (ключ = sha256(master_nsec + ":imap")), в БД
`imap_configs` (inbox.db) — только шифротекст.

### Демон (мульти-юзер)

```bash
# все включённые конфиги из БД + legacy-конфиг (если есть)
PYTHONPATH=src:deps python3 -m mailbridge.imap_bridge          # цикл, poll 120 c
PYTHONPATH=src:deps python3 -m mailbridge.imap_bridge --once   # один проход (для теста)
PYTHONPATH=src:deps python3 -m mailbridge.imap_bridge --no-legacy  # только БД
```

Статус каждой доставки пишется в `imap_configs.last_sync/last_error` —
виден пользователю во вкладке.

### Тесты

```bash
cd sites/cryter-mail && NO_BRIDGE=1 python3 -m pytest tests/test_imap_api.py -q
# + общий прогон: tests/ (56 тестов)
```
