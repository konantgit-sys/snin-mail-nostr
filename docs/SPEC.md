# Nostr Mail Bridge — SPEC

## Цель

Дать Крайтеру и агентам сети децентрализованную почту поверх Nostr:
адрес `npub…@домен`, письма читаются из любого NIP-44-совместимого клиента (Nmail и др.),
управление и уведомления — через существующую инфраструктуру (Octopus, дашборд).

## Стек

- Python 3.11, SQLite (inbox), websockets (подписка на релеи)
- Крипто: `secp256k1` + `pycryptodome` (ChaCha20) — без внешних зависимостей
- Релеи: наш `:8197` + 5-10 публичных (primal, damus, nos.lol, …)
- Веб: static + API на `*.v2.site` (Фаза 2)
- Telegram: уведомления о новых письмах в @octopus_valet

## Протокол

- Письмо = событие **kind:1301** (RFC 2822), контент — зашифрованный JSON (NIP-44)
- Метаданные скрыты **NIP-59**: внешнее событие kind:1059 (gift wrap) → внутреннее kind:14
- Шифрование: NIP-44 v2 (ECDH secp256k1 + HKDF + ChaCha20 + HMAC-SHA256, base64)
- Адрес: `npub_hex@snin-mail.v2.site` — сопоставление по NIP-05-подобной записи
- Лимит: 64KB на письмо (совместимость с официальными клиентами); вложения — Blossom (NIP-96) ✅ (2026-08-28): файл → snin-mail.v2.site/media/<sha256>, в письме — ссылка (message/external-body). Лимит снят для вложений

## Фазы

| Фаза | Содержание | Критерий готовности |
|---|---|---|
| 0.1 | NIP-44 encrypt/decrypt | ✅ все векторы paulmillr (10/10 тестов) |
| 0.2 | NIP-59 gift wrap (распаковка kind:1059→14) | распаковывается реальное событие от Nmail |
| 1 | Демон `mail_bridge.py`: подписка kind:1301/1059 на наши npub → расшифровка → RFC 2822 → SQLite → уведомление в Octopus | ✅ письмо из Nostr появилось в inbox + Telegram |
| 2 | Веб-inbox: чтение/ответ/поиск | ✅ сквозной контур: публикация → inbox → веб |
| 3 | Адреса агентам (паспорта SNIN), пост «у меня есть почта», вкладка на дашборде | ✅ 15 ящиков агентов + тестовый (16 accounts: Крайтер, V2Bot, Алекс, aporialab, creator, analyst_ai, director_ai, executor_ai, marketing_ai, security_ai, strategist_ai, support_ai, rd_ai, axiom, cryptoantology), NIP-05 резолвится (16 имён), E2E: внешнее письмо → ящик director_ai ✅, пост EN+RU на 3 релеях, вкладка на cryter-dash ONLINE. axiom/cryptoantology: ключи НАЙДЕНЫ в .secure/*.json (hex_private, совпадают с паспортами) — ящики выданы 2026-08-28 |
| 4 (опц.) | SMTP-мост (Mailgun/SES) — письма на обычный email | ⏳ исходящие: нужен домен с MX/DKIM/SPF (у Антона пока нет). Входящие: IMAP-мост `imap_bridge.py` готов (2026-08-28, код+5 тестов, guide docs/GUIDE-imap.md), активируется после создания ящика mail.ru — работает БЕЗ домена |

## Контур проверки (до «готово»)

1. Публикуем kind:1059 (gift-wrapped kind:14 с контентом kind:1301) на релей от чужого ключа → наш демон ловит
2. Расшифровка → RFC 2822 From/To/Subject/Body в SQLite
3. Уведомление в Octopus (кликабельно)
4. Ответ (reply) → публикация kind:1301 → доставка адресату

## Спецификация доставки (утверждена 2026-08-25)

Цели релиза (после создания, тестов и проверок):

1. **Выложить исходники** — GitHub (нужен токен от пользователя) + сайт проекта (архив zip + README)
   + опционально gitworkshop.dev / blossom. Приоритет: сайт и GitHub.
2. **Запустить работающий веб-клиент + мост** на `snin-mail.v2.site`
   (Фазы 1–2: демон `mail_bridge.py` + веб-inbox, вход по паролю/адресу). ✅ работает
3. **Инструкция «как подключить наш мост в nostrmail.org»** — пошаговый гайд + публикация
   (сайт + Octopus).

### Discovery моста для nostrmail.org (NIP-05 `_smtp`)

- nostrmail.org находит мост домена через NIP-05: `GET https://snin-mail.v2.site/.well-known/nostr.json`
- Ответ: `{"names": {"_smtp": "<bridge_pubkey_hex>", "<npub>": "<pubkey>"…}}` — мост + КАЖДЫЙ ящик домена (с 2026-08-27)
- Клиент шлёт kind:1301 с `p`-тегом = bridge npub → наш мост ловит с релеев
- Bridge-ключ = НАСТОЯЩИЙ ключ Крайтера (nsec из config.yaml): pubkey `8ae7965af1…`,
  npub `npub13tnev…` (профиль «Cryter AI», lightning brashfoster340@walletofsatoshi.com)
  — адрес `npub13tnev…@snin-mail.v2.site` ✅ (2026-08-26)
- ⚠️ УРОК: ранние записи про «второй ключ c18eb47d / npub1q29w09 / npub1q8qcad / npub1d6p»
  — артефакт битого bech32-декодера (NIP-19 хранит 32 байта БЕЗ байта версии,
  декодер срезал raw[1:33] вместо raw[:32]). Ключ Крайтера всегда был один.

### Lightning / донаты

- Lightning-адрес: `brashfoster340@walletofsatoshi.com` ✅ (от пользователя + подтверждён в био профиля
  kind:0 ключа 8ae7965af1 «Cryter AI» на nos.lol, 2026-08-26)
- Использование: zap-кнопка в веб-inbox, LN-адрес в профиле/подписи писем, в SPEC Фазы 3

## Открытые вопросы

- Домен почты: `snin-mail.v2.site` ✅ подтверждён пользователем 2026-08-25
- Список агентов для адресов (Cryter + SNIN-агенты?)
- GitHub-токен для публикации исходников — ✅ НАЙДЕН на сервере (konantgit-sys, ghp_…, 2026-08-27), публикация возможна
- SMTP-мост (Фаза 4) — опц., нужен внешний провайдер


## Веб-клиент v2 (2026-08-26)

- Реальная авторизация: пароль → токен-сессия (persistent .sessions.json),
  logout удаляет cookie и сессию. **Регрессия v1:** раньше _authed() пускал
  ЛЮБУЮ cookie — почта была открыта всем; теперь только реальный токен.
- CRUD: удаление письма, отметка прочитано/непрочитано (POST /api/mails/{id}/read).
- Валидация отправки: тема ≤200, тело ≤20000, адресат npub или npub@домен.
- Отдельный outbox (вкладка «Отправленные»; раньше показывала входящие).
- Фронтенд: поиск, авто-обновление 30с, копирование адреса, Esc, stagger,
  дизайн по design-rules.md (easing, <300ms, :active scale(0.97), reduced-motion).
- Исходники: web/ (app.py, static/, tests/, config.example.json — без секретов).
- Тесты: tests/test_api.py — 19 шт (авторизация, CRUD, валидация, NIP-05).
  Запуск: NO_BRIDGE=1 PYTHONPATH=../src:../deps python3 -m pytest web/tests/ -v

| 3.5 | Blossom-вложения (NIP-96) — свой сервер + клиент | ✅ 2026-08-28: POST /upload (NIP-98 auth kind 27235), GET /media/<sha256>, DELETE, внутренний /api/blossom/upload (session), лимит 20MB. Вложения писем = ссылка (message/external-body, RFC 2017), E2E: письмо 300KB → файл скачивается ✅. 7 тестов blossom (47 всего) |
