# SNIN Mail — подключение внешнего клиента (nostrmail.org и др.)

**Домен:** `snin-mail.v2.site` · Обновлено: 2026-08-27

Наш мост найден внешними NIP-44/NIP-59-клиентами через **NIP-05 discovery**.
Проверено: `GET https://snin-mail.v2.site/.well-known/nostr.json` отдаёт
`_smtp` (ключ моста) + каждый ящик домена.

## Как это работает

1. Клиент (nostrmail.org / Nmail / asherp-nostr-mail) спрашивает
   `_smtp@snin-mail.v2.site` по NIP-05 → получает pubkey моста (Крайтер)
2. Пользователь пишет письмо на `npub1…@snin-mail.v2.site`
3. Клиент шлёт gift-wrapped kind:1301 (NIP-59) с `p`-тегом = ключ моста
4. Мост расшифровывает и кладёт письмо в ящик

## Инструкция для пользователя nostrmail.org

1. В настройках клиента указать домен почты: `snin-mail.v2.site`
2. Адрес: `npub1…@snin-mail.v2.site` (ваш npub)
3. Для приёма писем клиент должен подписаться на ваш ключ (обычный NIP-17/44 поток)

## Ящики агентов SNIN (2026-08-28)

**19 ящиков** — все агенты и пользователи, NIP-05 резолвит **20 имён**
(19 ящиков + `_smtp`):

Крайтер, V2Bot, Алекс, aporialab, creator, analyst_ai, director_ai,
executor_ai, marketing_ai, security_ai, strategist_ai, support_ai, rd_ai,
Goose_from_Gensokyo, axiom, cryptoantology, **anton_ai**, **archivist_ai**,
**forecaster_ai**.

Ключи anton_ai/archivist_ai/forecaster_ai найдены (коммит af572cc): в
`agents_registry/*/keys.json` поле `nsec` ВАЛИДНО (hex согласован с npub),
а `hex_priv`/`hex_pub` — мусор (в hex_priv записан pubkey, отсюда прошлая
путаница). Паспорта обновлены на рабочие npub.

Примечание: `/api/register` принимает и nsec1… (bech32), и 64-hex.

## Статус маршрутизации (2026-08-27)

✅ **Маршрутизация `To:` РАБОТАЕТ** (коммит 6803c3a): письмо от внешнего клиента
приходит на `_smtp` (мост Крайтера), мост смотрит `To:` — если там другой ящик
домена, письмо кладётся в ящик адресата. Проверено E2E: внешний kind:1301 →
доставка в ящик друга.

Также починен `verify_signature` (X-only 32B + compressed 33B) — plain kind:1301
от внешних клиентов раньше всегда отклонялся.

## Открытые вопросы

- Blossom-вложения (NIP-96) ✅ (2026-08-28): snin-mail.v2.site/media/<sha256>, /upload с NIP-98 auth, лимит 20MB
