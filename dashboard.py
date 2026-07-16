# -*- coding: utf-8 -*-
"""
Captain Hook ダッシュボードサーバー v2
========================================
iPad/スマホのブラウザから観測状況を見るための閲覧専用Webサーバー。
- 追加ライブラリ不要(Python標準ライブラリのみ)
- 認証情報は同ディレクトリの dash_auth.txt から読む
  (形式: user:password の1行。このファイルはGitHubに上げないこと!)
- 売買機能は一切ない。DBとログを読むだけ。

[v2変更]
- Basic認証 → ログイン画面+Cookie方式に変更
  (ホーム画面Webアプリ化した際に毎回パスワードを求められる問題と
   起動時に黒画面になる問題への対応。初回ログイン後は1年間有効)
- ?v=2 のようなクエリ付きURLでも404にならないよう修正
- Basic認証も引き続き受け付ける(curlでの動作確認用)

起動:  venv/bin/python dashboard.py   (ポート 8787)
"""
import base64
import glob
import hashlib
import json
import os
import secrets
import threading
import time
import urllib.request
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from zoneinfo import ZoneInfo

import config
import db
import journal  # ローソク足取得(_hourly_candles_jst)を再利用する

JST = ZoneInfo("Asia/Tokyo")
PORT = 8787
AUTH_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "dash_auth.txt")
CAPITAL_JPY = 30000  # 表示用の運用資金
COOKIE_NAME = "chd_auth"
COOKIE_MAX_AGE = 60 * 60 * 24 * 365  # 1年


# ---------------------------------------------------------------
# 認証
# ---------------------------------------------------------------

def _load_cred() -> str:
    """dash_auth.txt から 'user:pass' を読む。無ければ起動を拒否"""
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
    return cred


CRED = _load_cred()
EXPECTED_BASIC = base64.b64encode(CRED.encode()).decode()
# Cookieに入れるトークン。認証情報から一方向ハッシュで生成する
# (パスワードを変えたら全端末で再ログインになる、という自然な挙動になる)
TOKEN = hashlib.sha256(("captain-hook-v2:" + CRED).encode()).hexdigest()


LOGIN_HTML = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Captain Hook">
<title>CAPTAIN HOOK — ログイン</title>
<style>
  :root{--paper:#F7F6F2;--ink:#17181A;--sub:#8A8D93;--line:#DCDAD2;--red:#C6403B}
  *{margin:0;padding:0;box-sizing:border-box}
  body{background:var(--paper);color:var(--ink);
    font-family:"SF Mono",Menlo,Consolas,"Hiragino Sans",monospace;
    min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px}
  .box{width:100%;max-width:360px;background:#fff;border:1.5px solid var(--ink);
    border-radius:4px;padding:28px 26px}
  .mark{width:34px;height:34px;border:1.5px solid var(--ink);border-radius:50%;
    display:flex;align-items:center;justify-content:center;margin-bottom:14px}
  h1{font-size:15px;letter-spacing:.05em;margin-bottom:2px}
  p.small{font-size:9px;color:var(--sub);letter-spacing:.16em;
    text-transform:uppercase;margin-bottom:22px}
  label{display:block;font-size:9px;color:var(--sub);letter-spacing:.14em;
    text-transform:uppercase;margin:14px 0 5px}
  input{width:100%;padding:11px 12px;font:inherit;font-size:14px;
    border:1px solid var(--line);border-radius:3px;background:var(--paper)}
  input:focus{outline:none;border-color:var(--ink)}
  button{width:100%;margin-top:22px;padding:12px;font:inherit;font-size:12px;
    font-weight:700;letter-spacing:.14em;color:#fff;background:var(--ink);
    border:none;border-radius:3px}
  .err{color:var(--red);font-size:11px;margin-top:12px;
    font-family:"Hiragino Sans",sans-serif}
</style>
</head>
<body>
<div class="box">
  <div class="mark">⚓</div>
  <h1>CAPTAIN HOOK</h1>
  <p class="small">BTC/JPY Observation — Sign in</p>
  <form method="POST" action="/login">
    <label>User</label>
    <input name="user" autocomplete="username" autocapitalize="none">
    <label>Password</label>
    <input name="pass" type="password" autocomplete="current-password">
    <button type="submit">SIGN IN</button>
  </form>
  __ERROR__
</div>
</body>
</html>"""


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

    # --- 検証ログ(直近7日分の全判定。no_trade以外の日も行を出す) ---
    # 当日はまだ夜間ジャーナル前で検証未集計のため対象外(date < today)
    cf_by_date = {r["date"]: r for r in cf_all}
    con = db.connect()
    plan_rows = con.execute(
        "SELECT date, regime, confidence FROM plans "
        "WHERE date < ? ORDER BY date DESC LIMIT 7", (today,)).fetchall()
    con.close()
    cf_log = []
    for pr in plan_rows:
        rec = cf_by_date.get(pr["date"]) or {}
        cf_log.append({
            "date": pr["date"],
            "regime": pr["regime"],
            "confidence": pr["confidence"],
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
    server_version = "CaptainHookDash/2.0"

    # --- 認証チェック ---
    def _has_valid_cookie(self) -> bool:
        raw = self.headers.get("Cookie", "")
        for part in raw.split(";"):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                if k == COOKIE_NAME and secrets.compare_digest(v, TOKEN):
                    return True
        return False

    def _has_valid_basic(self) -> bool:
        auth = self.headers.get("Authorization", "")
        return (auth.startswith("Basic ")
                and secrets.compare_digest(auth[6:], EXPECTED_BASIC))

    def _authorized(self) -> bool:
        return self._has_valid_cookie() or self._has_valid_basic()

    # --- 応答ヘルパー ---
    def _send(self, code: int, ctype: str, body: bytes,
              extra_headers: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _send_login(self, error: str = ""):
        err_html = (f'<p class="err">{error}</p>' if error else "")
        body = LOGIN_HTML.replace("__ERROR__", err_html).encode()
        self._send(200, "text/html; charset=utf-8", body,
                   {"Cache-Control": "no-store"})

    # --- ルーティング ---
    def do_GET(self):
        path = urlparse(self.path).path  # ?v=2 等のクエリは無視する

        if path == "/" or path.startswith("/index") or path == "/login":
            if not self._authorized():
                return self._send_login()
            try:
                with open(HTML_PATH, "rb") as f:
                    body = f.read()
            except FileNotFoundError:
                self.send_error(500, "dashboard.html not found")
                return
            self._send(200, "text/html; charset=utf-8", body,
                       {"Cache-Control": "no-store"})
        elif path == "/api/state":
            if not self._authorized():
                return self._send(401, "application/json; charset=utf-8",
                                  b'{"error": "unauthorized"}')
            body = json.dumps(get_state_cached(),
                              ensure_ascii=False).encode()
            self._send(200, "application/json; charset=utf-8", body,
                       {"Cache-Control": "no-store"})
        else:
            self.send_error(404)

    def do_POST(self):
        path = urlparse(self.path).path
        if path != "/login":
            return self.send_error(404)

        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(min(length, 4096)).decode(errors="replace")
        form = parse_qs(raw)
        user = (form.get("user") or [""])[0].strip()
        pw = (form.get("pass") or [""])[0]

        if secrets.compare_digest(f"{user}:{pw}", CRED):
            # ログイン成功: Cookieを付けてトップへリダイレクト
            self.send_response(303)
            self.send_header("Location", "/")
            self.send_header(
                "Set-Cookie",
                f"{COOKIE_NAME}={TOKEN}; Max-Age={COOKIE_MAX_AGE}; "
                f"Path=/; HttpOnly; SameSite=Lax")
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self._send_login("ユーザー名またはパスワードが違います")

    def log_message(self, fmt, *args):
        pass  # アクセスログは出さない


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Captain Hook dashboard v2: http://0.0.0.0:{PORT}")
    srv.serve_forever()
