import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import aiosqlite


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


SCHEMA = """
CREATE TABLE groups (
 id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL UNIQUE, token TEXT NOT NULL UNIQUE, title TEXT NOT NULL,
 currency TEXT NOT NULL DEFAULT 'MVR', minor_units INTEGER NOT NULL DEFAULT 2 CHECK(minor_units BETWEEN 0 AND 3),
 split_mode TEXT NOT NULL DEFAULT 'person' CHECK(split_mode IN ('person','wallet')),
 debt_mode TEXT NOT NULL DEFAULT 'simplified' CHECK(debt_mode IN ('simplified','direct')),
 pinned_message_id INTEGER, setup_message_id INTEGER, pin_warned INTEGER NOT NULL DEFAULT 0,
 pin_ok INTEGER NOT NULL DEFAULT 0, setup_done INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
);
CREATE TABLE wallets (
 id INTEGER PRIMARY KEY AUTOINCREMENT, group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
 settler_member_id INTEGER REFERENCES members(id), created_at TEXT NOT NULL, closed_at TEXT
);
CREATE TABLE members (
 id INTEGER PRIMARY KEY AUTOINCREMENT, group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
 tg_user_id INTEGER, name TEXT NOT NULL, username TEXT, weight TEXT NOT NULL DEFAULT '1',
 wallet_id INTEGER NOT NULL REFERENCES wallets(id), joined_at TEXT NOT NULL, left_at TEXT,
 UNIQUE(group_id, tg_user_id)
);
CREATE TABLE wallet_invites (
 id INTEGER PRIMARY KEY AUTOINCREMENT, group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
 from_member_id INTEGER NOT NULL REFERENCES members(id), to_member_id INTEGER NOT NULL REFERENCES members(id),
 from_wallet_id INTEGER NOT NULL REFERENCES wallets(id), to_wallet_id INTEGER NOT NULL REFERENCES wallets(id),
 status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','accepted','declined','cancelled')),
 created_at TEXT NOT NULL
);
CREATE TABLE expenses (
 id INTEGER PRIMARY KEY AUTOINCREMENT, group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
 amount_minor INTEGER NOT NULL CHECK(amount_minor > 0), description TEXT NOT NULL, category TEXT NOT NULL,
 payer_member_id INTEGER NOT NULL REFERENCES members(id), payer_wallet_id INTEGER NOT NULL REFERENCES wallets(id),
 logger_member_id INTEGER NOT NULL REFERENCES members(id), created_at TEXT NOT NULL,
 deleted_at TEXT, deleted_by INTEGER REFERENCES members(id), chat_message_id INTEGER,
 revision INTEGER NOT NULL DEFAULT 1, split_spec TEXT NOT NULL
);
CREATE TABLE expense_shares (
 expense_id INTEGER NOT NULL REFERENCES expenses(id) ON DELETE CASCADE,
 member_id INTEGER NOT NULL REFERENCES members(id), wallet_id INTEGER NOT NULL REFERENCES wallets(id),
 amount_minor INTEGER NOT NULL CHECK(amount_minor >= 0), PRIMARY KEY(expense_id, member_id)
);
CREATE TABLE transfers (
 id INTEGER PRIMARY KEY AUTOINCREMENT, group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
 kind TEXT NOT NULL CHECK(kind IN ('payment','opening','writeoff')),
 from_wallet_id INTEGER NOT NULL REFERENCES wallets(id), to_wallet_id INTEGER NOT NULL REFERENCES wallets(id),
 amount_minor INTEGER NOT NULL CHECK(amount_minor > 0), recorded_by INTEGER NOT NULL REFERENCES members(id),
 created_at TEXT NOT NULL, undone_at TEXT, undone_by INTEGER REFERENCES members(id), chat_message_id INTEGER,
 revision INTEGER NOT NULL DEFAULT 1, CHECK(from_wallet_id != to_wallet_id)
);
CREATE TABLE audit_log (
 id INTEGER PRIMARY KEY AUTOINCREMENT, group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
 member_id INTEGER REFERENCES members(id), action TEXT NOT NULL, entity TEXT NOT NULL,
 entity_id INTEGER, before_json TEXT, after_json TEXT, created_at TEXT NOT NULL
);
CREATE TABLE processed_messages (
 group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE, message_id INTEGER NOT NULL,
 PRIMARY KEY(group_id, message_id)
);
CREATE INDEX expenses_group ON expenses(group_id, created_at);
CREATE INDEX transfers_group ON transfers(group_id, created_at);
CREATE INDEX members_group ON members(group_id);
CREATE INDEX audit_group ON audit_log(group_id, id);
PRAGMA user_version=1;
"""


class Database:
    """Serialize transactions on a single connection; no awaits to Telegram inside a transaction."""

    def __init__(self, path):
        self.path = path
        self.lock = asyncio.Lock()

    async def open(self):
        self.conn = await aiosqlite.connect(self.path, isolation_level=None)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA foreign_keys=ON")
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.execute("PRAGMA busy_timeout=5000")
        version = (await self.one("PRAGMA user_version"))["user_version"]
        if version == 0:
            await self.conn.executescript("BEGIN IMMEDIATE;\n" + SCHEMA + "\nCOMMIT;")
        elif version != 1:
            raise RuntimeError(f"Unsupported database schema version {version}.")
        return self

    async def close(self):
        await self.conn.close()

    async def execute(self, sql, values=()):
        async with self.conn.execute(sql, values) as cursor:
            return cursor.lastrowid

    async def one(self, sql, values=()):
        async with self.conn.execute(sql, values) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def all(self, sql, values=()):
        async with self.conn.execute(sql, values) as cursor:
            return [dict(row) for row in await cursor.fetchall()]

    @asynccontextmanager
    async def transaction(self):
        async with self.lock:
            await self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self
            except BaseException:
                await self.conn.rollback()
                raise
            else:
                await self.conn.commit()

    async def audit(self, group_id, member_id, action, entity, entity_id, before=None, after=None):
        await self.execute(
            "INSERT INTO audit_log(group_id,member_id,action,entity,entity_id,before_json,after_json,created_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (
                group_id,
                member_id,
                action,
                entity,
                entity_id,
                json.dumps(before, ensure_ascii=False) if before is not None else None,
                json.dumps(after, ensure_ascii=False) if after is not None else None,
                now(),
            ),
        )
