"""SQLite: подключение с WAL и индексами (вертикальная оптимизация).

WAL: конкурентные чтения не блокируют запись моста; synchronous=NORMAL
для скорости при сохранении целостности. Индексы на горячие поля.
"""
from __future__ import annotations

import sqlite3

# один коннектор на поток (мост и веб-запросы — разные потоки)
_local = threading_local = None  # заглушка для старых интерпретаторов


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=15, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=15000")
    _ensure_indexes(conn)
    return conn


def _ensure_indexes(conn: sqlite3.Connection):
    """Индексы создаются один раз (IF NOT EXISTS) — дешёво на каждый коннект."""
    try:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_inbox_received ON inbox(received_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_inbox_read ON inbox(is_read)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_outbox_sent ON outbox(sent_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_inbox_owner_received ON inbox(owner, received_at DESC)")
        # черновики (фича: сохранение при закрытии композера)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS drafts ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " owner TEXT NOT NULL,"
            " to_addr TEXT DEFAULT '',"
            " subject TEXT DEFAULT '',"
            " body TEXT DEFAULT '',"
            " attachments TEXT DEFAULT '[]',"
            " updated_at INTEGER NOT NULL"
            ")"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_drafts_owner_updated ON drafts(owner, updated_at DESC)")
        # ── аудит удалений (08.09 письма были стёрты прямой SQL-операцией —
        #    следа не осталось; триггер фиксирует ЛЮБОЕ удаление в inbox,
        #    независимо от способа: веб-клиент, скрипт, ручной SQL) ──
        conn.execute(
            "CREATE TABLE IF NOT EXISTS audit_log ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " ts INTEGER NOT NULL,"
            " action TEXT NOT NULL,"       # delete | delete_request
            " table_name TEXT NOT NULL,"
            " row_id INTEGER,"
            " owner TEXT DEFAULT '',"
            " subject TEXT DEFAULT '',"
            " message_id TEXT DEFAULT '',"
            " received_at INTEGER,"
            " actor TEXT DEFAULT '',"      # кто инициировал (для API) или 'sql/trigger'
            " detail TEXT DEFAULT ''"      # доп. контекст (напр. сколько удалено)
            ")"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_owner ON audit_log(owner, ts DESC)")
        conn.execute(
            "CREATE TRIGGER IF NOT EXISTS trg_inbox_audit_del "
            "AFTER DELETE ON inbox BEGIN "
            "  INSERT INTO audit_log (ts, action, table_name, row_id, owner, subject,"
            "    message_id, received_at, actor) "
            "  VALUES (strftime('%s','now'), 'delete', 'inbox', OLD.id, OLD.owner,"
            "    OLD.subject, OLD.message_id, OLD.received_at, 'trigger'); "
            "END"
        )
        # архив (фича: папки) — колонка добавляется идемпотентно
        cols = {r[1] for r in conn.execute("PRAGMA table_info(inbox)").fetchall()}
        if "archived" not in cols:
            conn.execute("ALTER TABLE inbox ADD COLUMN archived INTEGER DEFAULT 0")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # таблиц ещё нет (первый запуск до миграции моста)


def query(db_path: str, sql: str, params: tuple = ()) -> list[dict]:
    """Короткий хелпер для чтения: коннект → запрос → закрыть."""
    with connect(db_path) as conn:
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]


def query_one(db_path: str, sql: str, params: tuple = ()) -> dict | None:
    """Одна строка (dict) или None."""
    rows = query(db_path, sql, params)
    return rows[0] if rows else None


def execute(db_path: str, sql: str, params: tuple = ()) -> int:
    """Короткий хелпер для записи: возвращает rowcount."""
    with connect(db_path) as conn:
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.rowcount
