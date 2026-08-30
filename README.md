# SNIN Mail — decentralized email on Nostr

Self-hosted, sovereign email built on Nostr. Messages are NIP-59 gift-wrapped
events, encryption is NIP-44 (v2), attachments live on Blossom (NIP-96).
Your keys, your mail, your relays — no servers, no surveillance.

This repository is the **unified project**: the web client, the protocol core
(NIP-44/59), the IMAP bridge and the Docker deploy in one place.

## Features

- **NIP-59 gift wrap** — sender hidden behind an ephemeral key, content
  encrypted twice (seal → gift wrap)
- **NIP-44 (v2)** — ECDH + HKDF + ChaCha20-Poly1305, verified against the
  official test vectors (`docs/nip44.vectors.json`)
- **NIP-96 Blossom** — chunked attachment upload, previews, progress
- **Multi-mailbox** — many accounts in one client, switch in the header
- **IMAP bridge** — import/read external mailboxes (`GUIDE-imap.md`)
- **Delivery queue** — relay subscriber → SQLite queue → worker pool;
  a message survives restarts, no loss on worker crash
- **Drafts** — auto-save when the composer closes
- **Archive** — archived folder (`archived`)
- **Docker one-command deploy** — `docker compose up -d` (`docker/`)
- **CLI** — send, read, draft, archive from the terminal (`scripts/mail_cli.py`)
- **Health monitoring** — RAM/disk/queue alerts to Telegram (`scripts/mail_health_monitor.py`)

## Architecture

```
Browser ──> FastAPI (uvicorn, 2 workers) :8123
              ├─ mailapp/routers/mail.py     — login, mails, send, drafts, archive
              ├─ mailapp/routers/blossom.py  — NIP-96 uploads
              ├─ mailapp/routers/imap.py     — IMAP bridge API
              ├─ mailapp/queue.py            — event queue (SQLite)
              ├─ mailapp/worker.py           — NIP-59 decryption outside WS perimeter
              └─ mailapp/bridge.py           — relay subscriber (no private keys)

mailbridge/                — protocol core: nip44, nip59, mail_message, blossom, imap_bridge
static/                    — esbuild bundle (long-cache fingerprints)
```

Three processes (`start.sh`): **bridge** (subscribe, no keys) → **worker**
(decrypt) → **uvicorn** (API). Private keys live only in the worker, encrypted
at rest with the master key.

## Repository layout

```
app.py                    — uvicorn entry point
build.py                  — esbuild bundle builder
mailapp/                  — backend: auth, bridge, config, db, queue, worker, imap_store
mailapp/routers/          — API: mail, blossom (NIP-96), imap
mailbridge/               — protocol core: nip44, nip59, mail_message, blossom, imap_bridge
docker/                   — Dockerfile + docker-compose.yml
docs/                     — SPEC.md, DEPLOY.md, NIP-44/59 specs, GUIDEs, test vectors
data/agents_registry/     — SNIN agent passports
scripts/                  — CLI, backups, health monitor, cache cleanup
static/                   — frontend sources + built bundle
tests/                    — 157 tests: API, protocol, queue, IMAP, drafts/archive
```

## Quick start

```bash
# 1. Config (example → production)
cp config.example.json config.json      # fill nsec/pubkey/relays/db

# 2. Install
pip3 install -r requirements.txt

# 3. Run
python3 -m uvicorn app:app --port 8123

# 4. Rebuild frontend after static/js/* edits
python3 build.py

# 5. Tests (self-contained: config.json is generated automatically)
python3 -m pytest tests/ -q             # 157 passed
```

### Docker

```bash
cd docker
docker compose up -d
```

## CLI

```bash
python3 scripts/mail_cli.py health | status
python3 scripts/mail_cli.py list                     # inbox
python3 scripts/mail_cli.py list --folder archive    # archive
python3 scripts/mail_cli.py read 42
python3 scripts/mail_cli.py send --to npub1…@snin-mail.v2.site \
    --subject "Hi" --body "Text" [--attach file.png]
python3 scripts/mail_cli.py draft --subject "Draft" --body "…"
python3 scripts/mail_cli.py archive 42 [--unarchive]
```

## API (main)

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

Auth: `Authorization: Bearer <token>`.

## Documentation

- `docs/SPEC.md` — full project specification
- `docs/NIP-44.md`, `docs/NIP-59.md` — protocol notes + test vectors
- `docs/GUIDE-nostrmail.md` — how SNIN Mail addresses work
- `docs/GUIDE-imap.md` — IMAP bridge setup
- `docs/GUIDE-friends.md` — friend-to-friend mail guide
- `docs/DEPLOY.md` — production deploy
- `docs/REFACTORING_SPEC.md` — phases 0–5 history

## History

This repo is the successor of **nostr-mail-bridge** (Aug 2026, archived):
the protocol core (NIP-44/59, mail format, IMAP bridge) was merged into the
web client and unified here. One project, one codebase, one deploy.

## Development rules

1. Tests are mandatory after every change: `pytest` (157, green)
2. Bundles are artifacts: after `static/js/*` edits run `build.py`
3. Secrets (`config.json`, `keys/`, `.sessions.json`) stay in `.gitignore`
4. Deploy and git mirror stay in sync (diff = 0)
