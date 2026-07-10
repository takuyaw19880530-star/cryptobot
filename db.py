# -*- coding: utf-8 -*-
"""SQLiteによる状態管理"""
import json
import os
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import config

JST = ZoneInfo("Asia/Tokyo")

SCHEMA = """
CREATE TABLE IF NOT EXISTS plans (
    date TEXT PRIMARY KEY,          -- YYYY-MM-DD (JST)
    regime TEXT,                    -- trend_up / trend_down / range / no_trade
    confidence REAL,
    caution_windows TEXT,           -- JSON: [["21:15","22:15"], ...]
    summary TEXT,
    raw_json TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT, ts_open TEXT, ts_close TEXT,
    side TEXT, size INTEGER,
    entry REAL, exit REAL, sl REAL, tp REAL,
    pl_pips REAL, pl_jpy REAL,
    regime TEXT, reason_open TEXT, reason_close TEXT,
    dry_run INTEGER
);
CREATE TABLE IF NOT EXISTS position (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    data TEXT                       -- 現在ポジションのJSON(無ければ行なし)
);
"""


def connect():
    d = os.path.dirname(config.DB_PATH)
    if d:
        os.makedirs(d, exist_ok=True)
    con = sqlite3.connect(config.DB_PATH)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def today_jst() -> str:
    # 暗号資産は24時間市場なので、日付境界は単純にJSTの0時とする
    return datetime.now(JST).strftime("%Y-%m-%d")


def save_plan(plan: dict):
    con = connect()
    con.execute(
        "INSERT OR REPLACE INTO plans VALUES (?,?,?,?,?,?,?)",
        (today_jst(), plan.get("regime"), plan.get("confidence"),
         json.dumps(plan.get("caution_windows", []), ensure_ascii=False),
         plan.get("summary"), json.dumps(plan, ensure_ascii=False),
         datetime.now(JST).isoformat()))
    con.commit()
    con.close()


def load_plan(date: str | None = None) -> dict | None:
    con = connect()
    row = con.execute("SELECT * FROM plans WHERE date=?",
                      (date or today_jst(),)).fetchone()
    con.close()
    if not row:
        return None
    plan = json.loads(row["raw_json"])
    plan["caution_windows"] = json.loads(row["caution_windows"] or "[]")
    return plan


def save_position(pos: dict | None):
    con = connect()
    if pos is None:
        con.execute("DELETE FROM position WHERE id=1")
    else:
        con.execute("INSERT OR REPLACE INTO position VALUES (1, ?)",
                    (json.dumps(pos, ensure_ascii=False),))
    con.commit()
    con.close()


def load_position() -> dict | None:
    con = connect()
    row = con.execute("SELECT data FROM position WHERE id=1").fetchone()
    con.close()
    return json.loads(row["data"]) if row else None


def record_trade(t: dict):
    con = connect()
    con.execute(
        """INSERT INTO trades (date, ts_open, ts_close, side, size, entry,
           exit, sl, tp, pl_pips, pl_jpy, regime, reason_open, reason_close,
           dry_run) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (t["date"], t["ts_open"], t["ts_close"], t["side"], t["size"],
         t["entry"], t["exit"], t["sl"], t["tp"], t["pl_pips"], t["pl_jpy"],
         t["regime"], t["reason_open"], t["reason_close"],
         1 if t["dry_run"] else 0))
    con.commit()
    con.close()


def trades_for(date: str) -> list[dict]:
    con = connect()
    rows = con.execute("SELECT * FROM trades WHERE date=? ORDER BY ts_open",
                       (date,)).fetchall()
    con.close()
    return [dict(r) for r in rows]


def pl_since(date_from: str) -> float:
    con = connect()
    row = con.execute("SELECT COALESCE(SUM(pl_jpy),0) s FROM trades "
                      "WHERE date>=?", (date_from,)).fetchone()
    con.close()
    return float(row["s"])


def daily_pl(date: str) -> float:
    return pl_since_between(date, date)


def pl_since_between(d1: str, d2: str) -> float:
    con = connect()
    row = con.execute("SELECT COALESCE(SUM(pl_jpy),0) s FROM trades "
                      "WHERE date BETWEEN ? AND ?", (d1, d2)).fetchone()
    con.close()
    return float(row["s"])
