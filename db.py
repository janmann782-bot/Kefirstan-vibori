from __future__ import annotations

import json
import sqlite3
import time

from config import DB_PATH, MODEL_ELECTORATE, PARTIES

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    first_name TEXT,
    region TEXT NOT NULL,
    voted_party TEXT,
    vote_weight REAL,
    voted_at REAL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS election (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    status TEXT NOT NULL DEFAULT 'closed',
    start_ts REAL,
    end_ts REAL,
    seed INTEGER NOT NULL DEFAULT 2059,
    last_tick_ts REAL,
    snapshot_no INTEGER NOT NULL DEFAULT 0,
    test_mode INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS region_state (
    region TEXT PRIMARY KEY,
    electorate INTEGER NOT NULL,
    turnout_target REAL NOT NULL DEFAULT 0.6,
    counted INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS counts (
    region TEXT NOT NULL,
    party TEXT NOT NULL,
    votes INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(region, party)
);

CREATE TABLE IF NOT EXISTS signals (
    region TEXT NOT NULL,
    party TEXT NOT NULL,
    weight REAL NOT NULL DEFAULT 0,
    PRIMARY KEY(region, party)
);

CREATE TABLE IF NOT EXISTS live_posts (
    chat_id INTEGER PRIMARY KEY,
    message_id INTEGER NOT NULL,
    chat_type TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS admin_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at REAL NOT NULL
);
"""


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    return con


async def init_db() -> None:
    with _connect() as db:
        db.executescript(SCHEMA)
        db.execute("INSERT OR IGNORE INTO election(id, status, seed) VALUES(1, 'closed', 2059)")
        for region in ("KAR", "TAR", "MAR"):
            db.execute(
                "INSERT OR IGNORE INTO region_state(region, electorate, turnout_target, counted) VALUES(?,?,?,0)",
                (region, MODEL_ELECTORATE[region], 0.6),
            )
            # При обновлении версии бота подтягиваем новый канонический размер электората
            # даже если elections.sqlite3 уже существовал со старыми тестовыми числами.
            db.execute(
                "UPDATE region_state SET electorate=? WHERE region=?",
                (MODEL_ELECTORATE[region], region),
            )
            for party in PARTIES:
                db.execute("INSERT OR IGNORE INTO counts(region, party, votes) VALUES(?,?,0)", (region, party))
                db.execute("INSERT OR IGNORE INTO signals(region, party, weight) VALUES(?,?,0)", (region, party))
        db.commit()


async def get_election() -> dict:
    with _connect() as db:
        row = db.execute("SELECT * FROM election WHERE id=1").fetchone()
        return dict(row)


async def set_election(**fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    with _connect() as db:
        db.execute(f"UPDATE election SET {cols} WHERE id=1", tuple(fields.values()))
        db.commit()


async def reset_election_data() -> None:
    with _connect() as db:
        db.execute("UPDATE counts SET votes=0")
        db.execute("UPDATE signals SET weight=0")
        db.execute("UPDATE region_state SET counted=0")
        db.execute("UPDATE users SET voted_party=NULL, vote_weight=NULL, voted_at=NULL")
        db.execute("UPDATE election SET status='closed', start_ts=NULL, end_ts=NULL, last_tick_ts=NULL, snapshot_no=0, test_mode=0 WHERE id=1")
        db.commit()


async def upsert_user(user_id: int, username: str | None, first_name: str | None, region: str) -> dict:
    now = time.time()
    with _connect() as db:
        db.execute(
            "INSERT OR IGNORE INTO users(user_id, username, first_name, region, created_at) VALUES(?,?,?,?,?)",
            (user_id, username, first_name, region, now),
        )
        db.execute("UPDATE users SET username=?, first_name=? WHERE user_id=?", (username, first_name, user_id))
        db.commit()
        return dict(db.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone())


async def get_user(user_id: int) -> dict | None:
    with _connect() as db:
        row = db.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        return dict(row) if row else None


async def set_user_region(user_id: int, region: str) -> bool:
    with _connect() as db:
        cur = db.execute("UPDATE users SET region=? WHERE user_id=?", (region, user_id))
        db.commit()
        return cur.rowcount > 0


async def cast_user_signal(user_id: int, party: str, weight: float) -> tuple[bool, dict | None]:
    now = time.time()
    db = _connect()
    try:
        db.execute("BEGIN IMMEDIATE")
        user = db.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        if not user or user["voted_party"]:
            db.rollback()
            return False, dict(user) if user else None
        region = user["region"]
        db.execute("UPDATE users SET voted_party=?, vote_weight=?, voted_at=? WHERE user_id=?", (party, weight, now, user_id))
        db.execute("UPDATE signals SET weight=weight+? WHERE region=? AND party=?", (weight, region, party))
        db.commit()
        return True, dict(user)
    finally:
        db.close()


async def revoke_user_vote(user_id: int) -> bool:
    db = _connect()
    try:
        db.execute("BEGIN IMMEDIATE")
        user = db.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        if not user or not user["voted_party"]:
            db.rollback()
            return False
        db.execute("UPDATE signals SET weight=MAX(0, weight-?) WHERE region=? AND party=?", (float(user["vote_weight"] or 0), user["region"], user["voted_party"]))
        db.execute("UPDATE users SET voted_party=NULL, vote_weight=NULL, voted_at=NULL WHERE user_id=?", (user_id,))
        db.commit()
        return True
    finally:
        db.close()


async def get_region_rows() -> dict[str, dict]:
    with _connect() as db:
        return {r["region"]: dict(r) for r in db.execute("SELECT * FROM region_state").fetchall()}


async def get_counts() -> dict[str, dict[str, int]]:
    out = {r: {p: 0 for p in PARTIES} for r in ("KAR", "TAR", "MAR")}
    with _connect() as db:
        for r in db.execute("SELECT region, party, votes FROM counts").fetchall():
            out[r["region"]][r["party"]] = int(r["votes"])
    return out


async def get_signals() -> dict[str, dict[str, float]]:
    out = {r: {p: 0.0 for p in PARTIES} for r in ("KAR", "TAR", "MAR")}
    with _connect() as db:
        for r in db.execute("SELECT region, party, weight FROM signals").fetchall():
            out[r["region"]][r["party"]] = float(r["weight"])
    return out


async def add_counts(region: str, additions: dict[str, int]) -> None:
    db = _connect()
    try:
        db.execute("BEGIN IMMEDIATE")
        total = 0
        for party, value in additions.items():
            value = max(0, int(value))
            if value:
                db.execute("UPDATE counts SET votes=votes+? WHERE region=? AND party=?", (value, region, party))
                total += value
        if total:
            db.execute("UPDATE region_state SET counted=counted+? WHERE region=?", (total, region))
        db.commit()
    finally:
        db.close()


async def inject_ballots(region: str, party: str, amount: int, admin_id: int) -> int:
    db = _connect()
    try:
        db.execute("BEGIN IMMEDIATE")
        rs = db.execute("SELECT electorate, counted FROM region_state WHERE region=?", (region,)).fetchone()
        max_extra = max(0, int(rs["electorate"] * 0.99) - int(rs["counted"]))
        actual = max(0, min(int(amount), max_extra))
        if actual:
            db.execute("UPDATE counts SET votes=votes+? WHERE region=? AND party=?", (actual, region, party))
            db.execute("UPDATE region_state SET counted=counted+? WHERE region=?", (actual, region))
        db.execute(
            "INSERT INTO admin_log(action,payload,created_at) VALUES(?,?,?)",
            ("inject_ballots", json.dumps({"admin": admin_id, "region": region, "party": party, "amount": actual}, ensure_ascii=False), time.time()),
        )
        db.commit()
        return actual
    finally:
        db.close()


async def add_signal(region: str, party: str, power: float, admin_id: int | None = None) -> None:
    with _connect() as db:
        db.execute("UPDATE signals SET weight=weight+? WHERE region=? AND party=?", (float(power), region, party))
        if admin_id is not None:
            db.execute(
                "INSERT INTO admin_log(action,payload,created_at) VALUES(?,?,?)",
                ("admin_signal", json.dumps({"admin": admin_id, "region": region, "party": party, "power": power}, ensure_ascii=False), time.time()),
            )
        db.commit()


async def set_turnout_target(region: str, value: float) -> None:
    value = max(0.01, min(0.99, float(value)))
    with _connect() as db:
        db.execute("UPDATE region_state SET turnout_target=? WHERE region=?", (value, region))
        db.commit()


async def bump_snapshot() -> int:
    with _connect() as db:
        db.execute("UPDATE election SET snapshot_no=snapshot_no+1 WHERE id=1")
        row = db.execute("SELECT snapshot_no FROM election WHERE id=1").fetchone()
        db.commit()
        return int(row[0])


async def add_live_post(chat_id: int, message_id: int, chat_type: str) -> None:
    with _connect() as db:
        db.execute(
            "INSERT INTO live_posts(chat_id,message_id,chat_type,active,created_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(chat_id) DO UPDATE SET message_id=excluded.message_id, chat_type=excluded.chat_type, active=1, created_at=excluded.created_at",
            (chat_id, message_id, chat_type, 1, time.time()),
        )
        db.commit()


async def get_live_posts() -> list[dict]:
    with _connect() as db:
        return [dict(r) for r in db.execute("SELECT * FROM live_posts WHERE active=1").fetchall()]


async def disable_live_post(chat_id: int) -> None:
    with _connect() as db:
        db.execute("UPDATE live_posts SET active=0 WHERE chat_id=?", (chat_id,))
        db.commit()


async def stats() -> dict:
    with _connect() as db:
        users = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        voted = db.execute("SELECT COUNT(*) FROM users WHERE voted_party IS NOT NULL").fetchone()[0]
        posts = db.execute("SELECT COUNT(*) FROM live_posts WHERE active=1").fetchone()[0]
        return {"users": users, "voted": voted, "live_posts": posts}
