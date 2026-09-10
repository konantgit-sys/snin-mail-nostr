"""Очередь писем (Фаза 4): подписчик кладёт сырое событие, воркеры расшифровывают.

Зачем: расшифровка NIP-59 (gift wrap) — CPU-bound. Если делать её в потоке
WebSocket подписчика, одно тяжёлое письмо блокирует приём остальных. Очередь
(SQLite) развязывает: подписчик делает только INSERT (миллисекунды), пул
воркеров расшифровывает параллельно и вне WS-периметра.

Гарантии:
- Очередь переживает рестарт (SQLite-файл, не память).
- Падение воркера не теряет письма: задача остаётся processing → reclaim_stale
  возвращает её в pending (таймаут RECLAIM_TIMEOUT).
- Гонка безопасна: claim использует UPDATE ... WHERE status='pending' —
  задачу забирает ровно один воркер (rowcount=1).
- Позднее завершение не портит чужую работу: claim выдаёт lease-токен, а
  finish матчит id + status='processing' + lease. Завершение «протухшего»
  владельца после reclaim_stale отклоняется (0 строк) и не тратит attempts
  нового владельца (см. tests/test_stale_completion.py).
- Воркер обрабатывает только владельцев своей группы (owner IN groups) плюс
  задачи без owner (owner='') — их пробует любой воркер.

Таблицы:
- mail_queue:  id, owner (pubkey получателя из тега p), payload (сырое событие),
               status (pending|processing|done|failed), attempts, worker,
               lease (токен владения, выдаётся claim и гасится при завершении),
               created_at, started_at, processed_at, error
- mail_workers: heartbeat воркеров (id, group_id, last_seen)
"""
from __future__ import annotations

import json
import secrets
import sqlite3
import time

from .config import DB

SCHEMA = """
CREATE TABLE IF NOT EXISTS mail_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner TEXT DEFAULT '',
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    worker TEXT DEFAULT '',
    lease TEXT DEFAULT '',
    created_at INTEGER NOT NULL,
    started_at INTEGER DEFAULT 0,
    processed_at INTEGER DEFAULT 0,
    error TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_mq_status_owner ON mail_queue(status, owner, id);
CREATE TABLE IF NOT EXISTS mail_workers (
    id TEXT PRIMARY KEY,
    group_id INTEGER NOT NULL DEFAULT 0,
    last_seen INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS mail_seen (
    event_id TEXT PRIMARY KEY,
    seen_at INTEGER NOT NULL
);
"""

SEEN_TTL = 7 * 86400  # храним id виденных событий 7 дней (дедуп повторов с релеев)

MAX_ATTEMPTS = 3       # попыток расшифровать до статуса failed
RECLAIM_TIMEOUT = 120  # сек: processing дольше этого срока → снова pending


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB, timeout=15)
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Догоняет схему для БД, созданных до появления lease (идемпотентно).

    H14 (внешнее ревью, board seq 29590): `ADD COLUMN lease TEXT DEFAULT ''`
    отдаёт pre-fix строкам lease=''. Такая строка остаётся status='processing',
    и завершение её вызовом finish() без lease делало бы контракт «вызов без
    lease отклоняется» ложным на границе миграции. Поэтому бесхозные строки
    (processing + lease='') явно возвращаются в pending: их заново выдаст
    claim(), который проставит свежий lease.

    Условие безопасности этого сброса: все воркеры прежней версии остановлены
    до миграции (docs/DEPLOY.md, «Upgrade: stop old workers before migrating»).
    Lease-предикат защищает только вызывающих текущей версии — finish() прежней
    версии матчит id + status и такую строку перезапишет.
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(mail_queue)").fetchall()}
    if "lease" not in cols:
        conn.execute("ALTER TABLE mail_queue ADD COLUMN lease TEXT DEFAULT ''")
        conn.execute(
            "UPDATE mail_queue SET status='pending', started_at=0, worker='', lease='' "
            "WHERE status='processing' AND lease=''"
        )


def ensure_schema() -> None:
    """Создаёт таблицы очереди, если их нет (идемпотентно)."""
    conn = _conn()
    try:
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.commit()
    finally:
        conn.close()


def _owner_of(event: dict) -> str:
    """Получатель письма — первый p-тег события.

    NIP-59 gift wrap (kind 1059) всегда несёт p = получатель; kind 1301 —
    p-теги адресатов. Если тега нет — owner='' (пробует любой воркер).
    """
    for t in event.get("tags", []) or []:
        if (
            isinstance(t, list) and t and t[0] == "p"
            and len(t) > 1 and isinstance(t[1], str) and len(t[1]) == 64
        ):
            return t[1]
    return ""


def _is_registered(pubkey: str) -> bool:
    """Есть ли в accounts ящик с таким ключом.

    Событие на незарегистрированный (или уже удалённый) npub — не письмо для
    нас: такие прилетают от чужих протоколов на публичные ключи из нашего
    NIP-05-списка. Если таблицы accounts нет (старая или тестовая БД) —
    ничего не фильтруем, чтобы не менять поведение вслепую.
    """
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT 1 FROM accounts WHERE pubkey_hex=? LIMIT 1", (pubkey,)
        ).fetchone()
    except sqlite3.OperationalError:
        return True
    finally:
        conn.close()
    return row is not None


def enqueue(event: dict) -> int | None:
    """Кладёт сырое событие в очередь (вызывает подписчик). Возвращает id.

    Дедупликация по event id: одно и то же письмо приходит с N релеев —
    в очередь попадает только первая копия (INSERT OR IGNORE в mail_seen).
    Возвращает None, если событие уже видели.
    """
    ensure_schema()
    eid = event.get("id", "")
    owner = _owner_of(event)
    if owner and not _is_registered(owner):
        # ящик снят или не зарегистрирован — не занимаем очередь мусором
        return None
    conn = _conn()
    try:
        if eid:
            cur = conn.execute(
                "INSERT OR IGNORE INTO mail_seen (event_id, seen_at) VALUES (?,?)",
                (eid, int(time.time())),
            )
            if cur.rowcount == 0:
                return None  # уже видели (повтор с другого релея)
        cur = conn.execute(
            "INSERT INTO mail_queue (owner, payload, status, created_at) VALUES (?,?, 'pending', ?)",
            (owner, json.dumps(event, ensure_ascii=False), int(time.time())),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def cleanup_seen(ttl: int = SEEN_TTL) -> int:
    """Удаляет старые записи mail_seen (вызывается воркером при старте)."""
    ensure_schema()
    cutoff = int(time.time()) - ttl
    conn = _conn()
    try:
        cur = conn.execute("DELETE FROM mail_seen WHERE seen_at < ?", (cutoff,))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def cleanup_workers(ttl: int = 600) -> int:
    """Удаляет heartbeat мёртвых воркеров (вызывается при старте воркера)."""
    ensure_schema()
    cutoff = int(time.time()) - ttl
    conn = _conn()
    try:
        cur = conn.execute("DELETE FROM mail_workers WHERE last_seen < ?", (cutoff,))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def claim(groups: set[str] | None = None, worker: str = "") -> dict | None:
    """Забирает одну задачу (race-safe: ровно один воркер).

    groups=None — любая pending-задача (для тестов/отладки).
    groups=set(pubkeys) — только задачи своих владельцев + owner=''.
    """
    ensure_schema()
    conn = _conn()
    try:
        if groups is None:
            rows = conn.execute(
                "SELECT id, owner, payload FROM mail_queue "
                "WHERE status='pending' ORDER BY id LIMIT 1"
            ).fetchall()
        else:
            ph = ",".join("?" * len(groups))
            rows = conn.execute(
                f"SELECT id, owner, payload FROM mail_queue "
                f"WHERE status='pending' AND (owner IN ({ph}) OR owner='') "
                f"ORDER BY id LIMIT 1",
                list(groups),
            ).fetchall()
        if not rows:
            return None
        rid, owner, payload = rows[0]
        lease = secrets.token_hex(16)  # токен владения: гасит завершение старого владельца
        cur = conn.execute(
            "UPDATE mail_queue SET status='processing', worker=?, started_at=?, lease=? "
            "WHERE id=? AND status='pending'",
            (worker, int(time.time()), lease, rid),
        )
        conn.commit()
        if cur.rowcount != 1:
            return None  # кто-то другой успел забрать
        return {"id": rid, "owner": owner, "payload": payload, "lease": lease}
    finally:
        conn.close()


def finish(rid: int, ok: bool, error: str = "", lease: str = "", permanent: bool = False) -> bool:
    """Завершение обработки: ok → done; иначе attempts+1 → pending|failed.

    permanent=True — отказ, который повтор не исправит: чужой kind внутри
    gift wrap, неразбираемый контент, дубликат. Такая задача помечается
    failed сразу, без трёх попыток. Причина правки — живой поток событий
    kind=25910 (чужой протокол) на наши ключи из NIP-05-списка: воркер
    тратил три расшифровки на событие, которое письмом не станет.

    Fencing: обновление матчит id + status='processing' + lease, поэтому
    завершение владельца, у которого задачу уже отобрал reclaim_stale,
    отклоняется (0 строк) — оно не трогает работу нового владельца и не
    списывает его attempts. attempts инкрементится атомарно внутри UPDATE,
    без предварительного SELECT.

    Возвращает True, если строка действительно обновлена этим владельцем.
    """
    if not lease:
        # H14: пустой lease — не владелец. Раньше дефолт lease='' совпадал с
        # мигрированной строкой (lease='' из ADD COLUMN DEFAULT) и завершал её,
        # хотя воркер её не брал. См. _migrate: бесхозные строки возвращаются в pending.
        return False
    conn = _conn()
    try:
        if ok:
            cur = conn.execute(
                "UPDATE mail_queue SET status='done', processed_at=?, error='', lease='' "
                "WHERE id=? AND status='processing' AND lease=?",
                (int(time.time()), rid, lease),
            )
        else:
            # permanent — повтор бессмыслен: сразу failed, попытки не жжём
            fence = "1=1" if permanent else f"attempts + 1 >= {MAX_ATTEMPTS}"
            cur = conn.execute(
                "UPDATE mail_queue SET status='failed', attempts=attempts+1, processed_at=?, "
                "error=?, lease='' WHERE id=? AND status='processing' AND lease=? "
                f"AND {fence}",
                (int(time.time()), error[:300], rid, lease),
            )
            if cur.rowcount != 1:
                cur = conn.execute(
                    "UPDATE mail_queue SET status='pending', attempts=attempts+1, started_at=0, "
                    "lease='', error=? WHERE id=? AND status='processing' AND lease=?",
                    (error[:300], rid, lease),
                )
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def reclaim_stale(timeout: int = RECLAIM_TIMEOUT) -> int:
    """Возвращает зависшие processing (воркер упал) обратно в pending."""
    ensure_schema()
    cutoff = int(time.time()) - timeout
    conn = _conn()
    try:
        cur = conn.execute(
            "UPDATE mail_queue SET status='pending', started_at=0, lease='' "
            "WHERE status='processing' AND started_at > 0 AND started_at < ?",
            (cutoff,),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def heartbeat(worker_id: str, group_id: int) -> None:
    """Пульс воркера (каждые ~10с) — для /api/health: workers_alive."""
    ensure_schema()
    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO mail_workers (id, group_id, last_seen) VALUES (?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET group_id=excluded.group_id, last_seen=excluded.last_seen",
            (worker_id, group_id, int(time.time())),
        )
        conn.commit()
    finally:
        conn.close()


def metrics() -> dict:
    """Метрики для /api/health. {} если БД/таблиц ещё нет (безопасно)."""
    try:
        ensure_schema()
    except Exception:
        return {}
    now = int(time.time())
    conn = _conn()
    try:
        pending = conn.execute("SELECT COUNT(*) FROM mail_queue WHERE status='pending'").fetchone()[0]
        processing = conn.execute("SELECT COUNT(*) FROM mail_queue WHERE status='processing'").fetchone()[0]
        done_1m = conn.execute(
            "SELECT COUNT(*) FROM mail_queue WHERE status='done' AND processed_at >= ?",
            (now - 60,),
        ).fetchone()[0]
        failed_1m = conn.execute(
            "SELECT COUNT(*) FROM mail_queue WHERE status='failed' AND processed_at >= ?",
            (now - 60,),
        ).fetchone()[0]
        failed_total = conn.execute("SELECT COUNT(*) FROM mail_queue WHERE status='failed'").fetchone()[0]
        workers_alive = conn.execute(
            "SELECT COUNT(*) FROM mail_workers WHERE last_seen >= ?", (now - 30,)
        ).fetchone()[0]
        workers_total = conn.execute("SELECT COUNT(*) FROM mail_workers").fetchone()[0]
    except Exception:
        return {}
    finally:
        conn.close()
    return {
        "pending": pending,
        "processing": processing,
        "done_1m": done_1m,
        "failed_1m": failed_1m,
        "failed_total": failed_total,
        "workers_alive": workers_alive,
        "workers_total": workers_total,
    }
