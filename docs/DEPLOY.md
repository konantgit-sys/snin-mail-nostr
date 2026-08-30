# Deploy: run Nostr Mail Bridge on your own server

Nostr Mail Bridge is a self-hosted, decentralized email system built on Nostr
protocol: mailboxes look like `npub…@your-domain`, messages are kind:1301
events on relays, encrypted with NIP-44, metadata hidden with NIP-59
(gift wrap). Protocol-compatible with NostrMail (nostrmail.org, Nmail client).

## Requirements

- A server (VPS) with public IP, 1 GB RAM is enough, Linux
- A **domain** you control (required for NIP-05 and for receiving mail
  from NostrMail clients that resolve addresses via DNS)
- A Nostr keypair (nsec/npub) — this will be the bridge owner (postmaster)
- Optional: an IMAP mailbox (mail.ru, Yandex, Gmail with app-password)
  to receive regular email into SNIN mailboxes
- Optional: Docker (recommended) or Python 3.10+

## Option A — Docker (recommended)

```bash
git clone https://github.com/konantgit-sys/nostr-mail-bridge.git
cd nostr-mail-bridge

# 1. config
cp web/config.example.json web/config.json
nano web/config.json          # fill nsec_hex, mail_domain, auth_password, relays

# 2. build & run
docker compose up -d --build

# 3. verify
curl http://localhost:8123/api/status
```

## Option B — bare metal (Python)

```bash
git clone https://github.com/konantgit-sys/nostr-mail-bridge.git
cd nostr-mail-bridge

python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp web/config.example.json web/config.json
nano web/config.json

# web inbox (FastAPI)
cd web && PYTHONPATH=../src python3 -m uvicorn app:app --host 0.0.0.0 --port 8123

# IMAP bridge daemon (separate process)
PYTHONPATH=src python3 -m mailbridge.imap_bridge --no-legacy
```

## Configuration (`web/config.json`)

| Field | Meaning |
|---|---|
| `nsec_hex` | private key of the bridge owner (postmaster) |
| `pubkey_hex` / `npub` | public key of the bridge owner |
| `mail_domain` | your domain, e.g. `mail.example.com` |
| `mail_address` | `npub…@mail.example.com` |
| `relays` | list of relays (default: public ones) |
| `db` | SQLite path (relative to web/ or absolute) |
| `auth_password` | admin password for the web UI |
| `telegram_token` / `telegram_chat_id` | optional Telegram notifications |
| `owners` | list of mailbox owners (each with own nsec_hex) |
| `limits` | quotas (max mails per user, send per day, attachment size…) |

Every user can also register their own mailbox from the web UI
(`/api/register` with their nsec) — no admin needed.

## NIP-05 (receiving mail from external NostrMail clients)

1. Point `mail_domain` to your server. The app serves
   `/.well-known/nostr.json` automatically — it lists all registered
   mailboxes with their pubkeys.
2. Make sure `https://mail.example.com/.well-known/nostr.json` is reachable
   (DNS A-record to your server; reverse proxy must not block the path).
3. Test: `curl https://mail.example.com/.well-known/nostr.json?name=npub…`

NIP-05 resolves `npub…@mail.example.com` → pubkey. Clients (nostrmail.org,
Nmail) then find the relay list and send kind:1301 gift-wrapped to that key.

## IMAP bridge (regular email → SNIN mailboxes)

Each user can connect their own IMAP mailbox from the UI tab
«Входящие (IMAP)»: host/port/SSL/username + app-password. The bridge
fetches mail, wraps it as kind:1301 (signed by the owner's key), publishes
to relays, and the owner's bridge delivers it to their inbox.

The daemon reads configs from the `imap_configs` table (added via the UI),
passwords are stored AES-256-GCM encrypted.

```bash
PYTHONPATH=src python3 -m mailbridge.imap_bridge --no-legacy
```

Note: port 993/995 (IMAP/POP3 over TLS) must be allowed on the server
(outbound). Some sandboxes/hosting block mail ports — check with
`curl -v imaps://imap.mail.ru` before debugging.

## Tests

```bash
pip install -r requirements.txt
cd web && python3 -m pytest tests -q          # web API tests (incl. IMAP)
python3 -m pytest tests -q                    # bridge/NIP tests
```

## Backups

The only state is the SQLite database (`db` path, default `web/inbox.db`).
Back it up regularly:

```bash
sqlite3 web/inbox.db ".backup backup-$(date +%F).db"
```

To restore, stop the app, replace the file, start again.

## Updating

```bash
git pull
docker compose up -d --build        # Docker
# or: pip install -r requirements.txt && restart services
```
