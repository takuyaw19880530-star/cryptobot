# -*- coding: utf-8 -*-
"""
Captain Hook ダッシュボードサーバー
====================================
iPad/スマホのブラウザから観測状況を見るための閲覧専用Webサーバー。
- 追加ライブラリ不要(Python標準ライブラリのみ)
- Basic認証つき。認証情報は同ディレクトリの dash_auth.txt から読む
  (形式: user:password の1行。このファイルはGitHubに上げないこと!)
- 売買機能は一切ない。DBとログを読むだけ。

起動:  venv/bin/python dashboard.py   (ポート 8787)
"""
import base64
import glob
import json
import os
import secrets
import threading
import time
import urllib.request
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

import config
import db
import journal  # ローソク足取得(_hourly_candles_jst)を再利用する

JST = ZoneInfo("Asia/Tokyo")
PORT = 8787
AUTH_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "dash_auth.txt")
CAPITAL_JPY = 30000  # 表示用の運用資金


# ---------------------------------------------------------------
# 認証
# ---------------------------------------------------------------

def _load_auth() -> str:
    """dash_auth.txt から 'user:pass' を読み、Basic認証用の
    base64文字列を返す。ファイルが無ければ起動を拒否する"""
    try:
        with open(AUTH_FILE, encoding="utf-8") as f:
            cred = f.read().strip()
    except FileNotFoundError:
        raise SystemExit(
            f"[ERROR] {AUTH_FILE} がありません。\n"
            "VPS上で次を実行してから起動してください(パスワードは自分で決める):\n"
            "  echo 'takuya:ここにパスワード' > dash_auth.txt")
    if ":" not in cred:
        raise SystemExit("[ERROR] dash_auth.txt は user:password 形式で"
                         "1行で書いてください")
    return base64.b64encode(cred.encode()).decode()


EXPECTED_AUTH = _load_auth()


# ---------------------------------------------------------------
# データ収集(60秒キャッシュ)
# ---------------------------------------------------------------

_cache = {"ts": 0.0, "data": None}
_cache_lock = threading.Lock()
_fng_cache = {"ts": 0.0, "data": None}


def _fetch_fng() -> dict | None:
    """Fear & Greed指数(alternative.me, 認証不要)。10分キャッシュ"""
    if time.time() - _fng_cache["ts"] < 600 and _fng_cache["data"]:
        return _fng_cache["data"]
    try:
        req = urllib.request.Request(
            "https://api.alternative.me/fng/?limit=1",
            headers={"User-Agent": "cryptobot-dash"})
        with urllib.request.urlopen(req, timeout=8) as r:
            j = json.loads(r.read().decode())
        d = j["data"][0]
        out = {"value": int(d["value"]),
               "label": d.get("value_classification", "")}
        _fng_cache.update(ts=time.time(), data=out)
        return out
    except Exception:
        return _fng_cache["data"]


def _cf_avg_pl(rec: dict) -> float | None:
    """counterfactual記録1件のトレンド/レンジ仮想損益の平均"""
    vals = [rec[k]["pl_jpy"] for k in ("trend_params", "range_params")
            if rec.get(k)]
    return sum(vals) / len(vals) if vals else None


def _load_cf_all() -> list[dict]:
    path = os.path.join(config.JOURNAL_DIR, "counterfactual.jsonl")
    if not os.path.exists(path):
        return []
    by_date: dict[str, dict] = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                by_date[rec["date"]] = rec
            except (json.JSONDecodeError, KeyError):
                continue
    return [by_date[d] for d in sorted(by_date)]


def _obs_days() -> int:
    con = db.connect()
    n = con.execute("SELECT COUNT(*) c FROM plans").fetchone()["c"]
    con.close()
    return int(n)


def _trades_total() -> int:
    con = db.connect()
    n = con.execute("SELECT COUNT(*) c FROM trades").fetchone()["c"]
    con.close()
    return int(n)


def build_state() -> dict:
    today = db.today_jst()
    now = datetime.now(JST)

    # --- ローソク足(今日 + 昨日。24h変化率の計算にも使う) ---
    candles_today = journal._hourly_candles_jst(today)
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    candles_yday = journal._hourly_candles_jst(yesterday)

    last_price = candles_today[-1]["close"] if candles_today else None
    chg24 = None
    if last_price:
        target = now - timedelta(hours=24)
        pool = candles_yday + candles_today
        past = min(pool, key=lambda c: abs((c["t"] - target).total_seconds()),
                   default=None)
        if past and past["close"]:
            chg24 = (last_price - past["close"]) / past["close"] * 100

    day_high = max((c["high"] for c in candles_today), default=None)
    day_low = min((c["low"] for c in candles_today), default=None)

    # --- 本日の作戦 ---
    plan = db.load_plan(today) or {}

    # --- 本日の仮想トレード(no_trade日のみ) ---
    virtual = None
    if plan.get("regime") == "no_trade" and candles_today and last_price:
        entry_candle = next(
            (c for c in candles_today if c["t"].hour >= journal.CF_ENTRY_HOUR),
            None)
        if entry_candle:
            entry = entry_candle["open"]
            virtual = {
                "entry": round(entry),
                "pl_now": round((last_price - entry) / entry
                                * config.ORDER_JPY),
            }

    # --- KPI ---
    cf_all = _load_cf_all()
    avg_list = [(_cf_avg_pl(r), r) for r in cf_all]
    avg_list = [(a, r) for a, r in avg_list if a is not None]
    avoided_total = -round(sum(a for a, _ in avg_list))
    hit_ok = sum(1 for a, _ in avg_list if a <= 0)

    kpi = {
        "realized_total": round(db.pl_since("2000-01-01")),
        "avoided_total": avoided_total,
        "hit": f"{hit_ok}/{len(avg_list)}" if avg_list else "—",
        "capital": CAPITAL_JPY,
        "obs_days": _obs_days(),
        "trades_total": _trades_total(),
    }

    # --- 検証ログ(直近7件, 新しい順) ---
    cf_log = []
    for rec in cf_all[-7:][::-1]:
        p = db.load_plan(rec["date"]) or {}
        cf_log.append({
            "date": rec["date"],
            "confidence": p.get("confidence"),
            "trend_pl": (rec.get("trend_params") or {}).get("pl_jpy"),
            "range_pl": (rec.get("range_params") or {}).get("pl_jpy"),
            "exit_reason": (rec.get("trend_params") or {}).get("exit_reason"),
        })

    return {
        "now": now.isoformat(),
        "mode": "DRY_RUN" if getattr(config, "DRY_RUN", True) else "LIVE",
        "price": {"last": round(last_price) if last_price else None,
                  "chg24h_pct": round(chg24, 2) if chg24 is not None else None,
                  "day_high": round(day_high) if day_high else None,
                  "day_low": round(day_low) if day_low else None},
        "candles_today": [{"h": c["t"].hour + c["t"].minute / 60,
                           "close": c["close"]} for c in candles_today],
        "plan": {"regime": plan.get("regime"),
                 "confidence": plan.get("confidence"),
                 "summary": plan.get("summary"),
                 "key_events": plan.get("key_events", []),
                 "caution_windows": plan.get("caution_windows", [])},
        "virtual": virtual,
        "fng": _fetch_fng(),
        "kpi": kpi,
        "cf_log": cf_log,
    }


def get_state_cached() -> dict:
    with _cache_lock:
        if time.time() - _cache["ts"] > 55 or _cache["data"] is None:
            try:
                _cache["data"] = build_state()
                _cache["ts"] = time.time()
            except Exception as e:
                if _cache["data"] is None:
                    _cache["data"] = {"error": str(e)}
        return _cache["data"]


# ---------------------------------------------------------------
# HTTPサーバー
# ---------------------------------------------------------------

HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "dashboard.html")


class Handler(BaseHTTPRequestHandler):
    server_version = "CaptainHookDash/1.0"

    def _authorized(self) -> bool:
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Basic "):
            return False
        return secrets.compare_digest(auth[6:], EXPECTED_AUTH)

    def _deny(self):
        self.send_response(401)
        self.send_header("WWW-Authenticate",
                         'Basic realm="Captain Hook", charset="UTF-8"')
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write("認証が必要です".encode())

    def do_GET(self):
        if not self._authorized():
            return self._deny()

        if self.path == "/" or self.path.startswith("/index"):
            try:
                with open(HTML_PATH, "rb") as f:
                    body = f.read()
            except FileNotFoundError:
                self.send_error(500, "dashboard.html not found")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/state":
            body = json.dumps(get_state_cached(),
                              ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type",
                             "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def log_message(self, fmt, *args):
        pass  # アクセスログは出さない(cron.logを汚さないため)


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Captain Hook dashboard: http://0.0.0.0:{PORT}")
    srv.serve_forever()
