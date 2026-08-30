# SNIN Mail — Web Client

[![CI](https://img.shields.io/github/actions/workflow/status/konantgit-sys/snin-mail-nostr/tests.yml?branch=main&label=CI)](https://github.com/konantgit-sys/snin-mail-nostr/actions)
[![Tests](https://img.shields.io/badge/tests-115%20passed-00e0f0)](tests/)
[![Python](https://img.shields.io/badge/python-3.11-3776AB)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-AGPL--3.0-blue)](LICENSE)

**Decentralized email on the Nostr protocol.** Messages are NIP-59 gift-wrap
events; storage is your keys; attachments go to Blossom (NIP-96). A FastAPI
backend + an esbuild-bundled frontend in the SNIN Network visual style
(aurora background, glass panels, neon #00e0f0).

Self-hosted, private, censorship-resistant mail. Mailboxes look like
`npub…@your-domain`.

## Screenshots

| Login | Inbox | Message |
|---|---|---|
| ![Login](docs/screenshots/login.png) | ![Inbox](docs/screenshots/inbox.png) | ![Message](docs/screenshots/detail.png) |

## Features

- **NIP-59** — messages are encrypted on sender/receiver keys (gift wrap)
- **Multi-mailbox** — 24+ accounts in one client, switch in the header
- **Blossom attachments** — chunked upload, previews, progress (NIP-96)
- **IMAP bridge** — import/read external mailboxes
- **Drafts** — autosave when the composer closes (`drafts`)
- **Archive** — archived folder (`archived`)
- **Delivery queue** — bridge → queue → workers; a message survives restarts
- **Premium design** — aurora background, glass panels, neon #00e0f0, mobile-first

## Architecture

```
Browser ──> :8123 FastAPI (uvicorn, 2 workers)
              ├─ routers/mail.py     — API: login, mails, send, drafts, archive
              ├─ routers/blossom.py  — NIP-96: attachment upload, chunks
              ├─ routers/imap.py     — IMAP bridge
              ├─ mailapp/queue.py    — event queue (SQLite)
              ├─ mailapp/worker.py   — NIP-59 decryption outside the WS perimeter
              └─ mailapp/bridge.py   — relay subscriber (separate process)
Static: static/index.html + app.<hash>.js/css (esbuild, long-cache)
```

Three processes (`start.sh`): **bridge** (subscription, no keys) → **worker**
(decryption) → **uvicorn** (API). Keys live only in the worker, stored in
`mail_keys` (encrypted with master.key).

## Dependency: nostr-mail-bridge

The client talks to the Nostr network through the `mailbridge` package
([konantgit-sys/nostr-mail-bridge](https://github.com/konantgit-sys/nostr-mail-bridge)),
imported at runtime. Add its `src/` to `PYTHONPATH`:

```bash
git clone https://github.com/konantgit-sys/nostr-mail-bridge.git
export PYTHONPATH=$PWD/nostr-mail-bridge/src:$PWD/nostr-mail-bridge/deps
```

Or set `NOSTR_MAIL_BRIDGE_SRC=/path/to/nostr-mail-bridge/src` — tests pick it up too.

## Quick start

```bash
# 1. Config (example → real)
cp config.example.json config.json   # fill nsec/pubkey/relays/db

# 2. Backend
python3 -m uvicorn app:app --port 8123

# 3. Frontend (rebuild bundle after editing static/js/*)
python3 build.py                     # needs esbuild (npm install esbuild)

# 4. Tests (self-contained: conftest generates config.json automatically)
python3 -m pytest tests/ -q          # 115 passed
```

**Self-contained tests:** in a fresh clone without `config.json`, conftest.py
creates a test config itself (random paired nsec/pubkey, local DB, empty
relays). The production config.json is never overwritten.

## CLI

```bash
python3 scripts/mail_cli.py health                    # metrics
python3 scripts/mail_cli.py list                      # inbox
python3 scripts/mail_cli.py list --folder archive     # archive
python3 scripts/mail_cli.py list --folder outbox      # sent
python3 scripts/mail_cli.py read 42                   # detail + body
python3 scripts/mail_cli.py send --to npub1…@your-domain \
    --subject "Hello" --body "Text" [--attach file.png]
python3 scripts/mail_cli.py draft --subject "Draft" --body "…"
python3 scripts/mail_cli.py archive 42                # to archive
```

Options: `--url http://localhost:8123`, `--password` (or env `MAIL_PASSWORD`),
`--token` (env `MAIL_TOKEN`; token cached in `~/.cache/mail_cli_token`).

## API

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/login` | login (nsec or password) → token |
| GET | `/api/mails?folder=` | list (inbox/archive), 5s cache |
| GET | `/api/mails/{id}` | detail + mark read |
| POST | `/api/mails/{id}/read` | read/unread |
| POST | `/api/mails/{id}/archive` | `{"archived": true\|false}` |
| DELETE | `/api/mails/{id}` | delete |
| POST | `/api/send` | send |
| POST | `/api/drafts` | save draft |
| GET | `/api/outbox` | sent |
| POST | `/api/blossom/upload` | attachment (NIP-96) |
| GET | `/api/health` | RAM, DB, queue metrics |

Auth: `Authorization: Bearer <token>` (cookies are ignored — the v2.site
proxy caches Set-Cookie).

## Repository layout

```
app.py                     — uvicorn entry point
build.py                   — esbuild frontend build (fingerprint bundles)
mailapp/                   — backend: auth, bridge, config, db, queue, worker, imap_store
mailapp/routers/           — API: mail, blossom (NIP-96), imap
static/js/                 — frontend sources: core, api, inbox, detail, composer, main
static/templates/index.src.html — markup (built → static/index.html)
tests/                     — 115 tests: API, Blossom, queue, IMAP, drafts/archive
scripts/                   — CLI, backups, health monitor, cache cleanup
docs/screenshots/          — screenshots used in this README
```

## License

[GNU AGPL-3.0](LICENSE) — server-side software, keep the network free.
