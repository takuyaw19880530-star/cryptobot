# -*- coding: utf-8 -*-
"""
夜の日誌 & 週次反省会
=====================
- journal: 当日の作戦とトレードをClaudeが振り返りMarkdown日誌に記録
- review : 直近1週間の日誌と成績からパラメータ変更を「提案」する。
           自動では何も変えない。人間がconfig.pyを書き換えて初めて反映。

[v2追加] 反実仮想(counterfactual)検証
- no_trade日に「もし朝9時にロングしていたら」の仮想損益を計算し、
  counterfactual.jsonl に記録 & 日誌に反映する。
- 週次反省会で週間の回避損益/機会損失を集計する。
- これによりレジーム判定の的中率を実額で裏付けられるようにする。
- 注意: 執行エンジンの完全再現ではなく単純化ベンチマーク
  (9:00成行ロング → SL/TP → 引けで手仕舞い)。手数料は含まない。
"""
import glob
import json
import os
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import anthropic

import config
import db
import notify

JST = ZoneInfo("Asia/Tokyo")
UTC = ZoneInfo("UTC")

JOURNAL_SYSTEM = """あなたはBTC/JPY現物自動売買botのトレード日誌を書く担当です。
渡された「朝の作戦」と「当日の全トレード」を突き合わせて、以下の構成の
Markdown日誌を日本語で書いてください。感想ではなく検証を書くこと。

# YYYY-MM-DD トレード日誌
## 本日の成績
## 朝のレジーム判定は当たったか
## ルール通りに執行できたか
## no_trade検証(該当日のみ)
「仮想トレード結果」が渡された場合のみこの節を書く。no_tradeにより
回避できた損失、または逃した利益を実額で淡々と記述する。
仮想損益がプラスだった日を「判定ミス」と断定せず、リスクとリターンの
両面から評価すること。データ取得失敗の場合はその旨を1行書く。
## 気づき(次に活かす具体的な1点)
## 📘 今日の用語解説
本文で使った専門用語や相場概念を1つ選び、投資初心者向けに
たとえ話を交えて3文以内で解説する。

全体で600字以内。誇張せず、負けた日は負けた事実を淡々と分析すること。"""

REVIEW_SYSTEM = """あなたはBTC/JPY現物自動売買botの週次レビュー担当です。
1週間分の日誌と成績データから、以下をMarkdownで出力してください。

# 週次反省会 (WEEK)
## 今週の総括(勝率・平均損益・レジーム判定の的中率)
「仮想トレード集計」が渡された場合、レジーム判定の的中率は
主観評価ではなく回避損益の実額を根拠にすること。
(例: no_trade判定により仮想損失 -X円 を回避 / 機会損失 +Y円)
## うまくいったパターン / いかなかったパターン
## パラメータ変更の提案(最大2つまで)
提案は「変更対象のconfig.py変数名 / 現在値 / 提案値 / 根拠」の形式で。
根拠がデータで示せない変更は提案しないこと。変更ゼロという結論も歓迎。
最後に必ず「※この提案は自動反映されません。納得した場合のみconfig.pyを
手動で変更してください」と書くこと。"""

# 仮想トレードのエントリー時刻(JST)。朝のレジーム判定の直後を想定。
CF_ENTRY_HOUR = 9


def _client():
    return anthropic.Anthropic()


def _target_date() -> str:
    """深夜(6時前)の実行は前日分を対象にする(cron 0:05運用のため)"""
    now = datetime.now(JST)
    if now.hour < 6:
        now -= timedelta(days=1)
    return now.strftime("%Y-%m-%d")


# ---------------------------------------------------------------
# 反実仮想(counterfactual)検証
# ---------------------------------------------------------------

def _pct(v: float) -> float:
    """config値が 1.5(%表記) でも 0.015(小数) でも小数に正規化する。
    1以上なら%表記とみなして100で割る(SL/TPが100%を超える設定は想定しない)"""
    return v / 100.0 if v >= 1 else v


def _fetch_candle_file(yyyymmdd: str) -> list:
    """bitbank公開APIから1時間足の1日分ファイルを取得する(認証不要)。
    失敗時は空リストを返す"""
    url = f"https://public.bitbank.cc/btc_jpy/candlestick/1hour/{yyyymmdd}"
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "cryptobot-journal"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode())
        if data.get("success") != 1:
            return []
        return data["data"]["candlestick"][0]["ohlcv"]
    except Exception:
        return []


def _hourly_candles_jst(date: str) -> list[dict]:
    """対象日(JST 0:00〜23:59)の1時間足を取得する。
    bitbankの日付キーはUTC基準のため、JST日の開始/終了が属する
    UTC日付の2ファイルを取得してJSTでフィルタする"""
    day_start = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=JST)
    day_end = day_start + timedelta(days=1)

    utc_days = {
        day_start.astimezone(UTC).strftime("%Y%m%d"),
        (day_end - timedelta(hours=1)).astimezone(UTC).strftime("%Y%m%d"),
    }
    raw = []
    for d in sorted(utc_days):
        raw += _fetch_candle_file(d)

    out = []
    for row in raw:
        try:
            o, h, l, c, _v, ts = row
            t = datetime.fromtimestamp(int(ts) / 1000, JST)
        except (ValueError, TypeError):
            continue
        if day_start <= t < day_end:
            out.append({"t": t, "open": float(o), "high": float(h),
                        "low": float(l), "close": float(c)})
    out.sort(key=lambda x: x["t"])
    return out


def _simulate_long(candles: list[dict], sl_pct: float, tp_pct: float,
                   order_jpy: float) -> dict | None:
    """CF_ENTRY_HOUR時の始値でロング → SL/TP到達で決済、
    未到達なら当日最終足の終値で決済する単純シミュレーション。
    同一足内でSL/TP両方に到達し得る場合はSL優先(保守的評価)。
    candlesが空、またはエントリー足が無い場合はNone"""
    entry_candle = next(
        (c for c in candles if c["t"].hour >= CF_ENTRY_HOUR), None)
    if entry_candle is None:
        return None

    entry = entry_candle["open"]
    sl_price = entry * (1 - _pct(sl_pct))
    tp_price = entry * (1 + _pct(tp_pct))

    exit_price = candles[-1]["close"]
    exit_reason = "day_close"
    for c in candles:
        if c["t"] < entry_candle["t"]:
            continue
        if c["low"] <= sl_price:
            exit_price, exit_reason = sl_price, "stop_loss"
            break
        if c["high"] >= tp_price:
            exit_price, exit_reason = tp_price, "take_profit"
            break

    pl = (exit_price - entry) / entry * order_jpy
    return {
        "entry": round(entry),
        "exit": round(exit_price),
        "exit_reason": exit_reason,
        "pl_jpy": round(pl),
    }


def counterfactual_for(date: str) -> dict | None:
    """no_trade日の仮想トレード結果を計算する。
    トレンド用/レンジ用の両パラメータセットで試算する。
    ローソク足が取得できない場合はNone"""
    candles = _hourly_candles_jst(date)
    if not candles:
        return None

    trend = _simulate_long(candles, config.TREND_SL_PCT,
                           config.TREND_TP_PCT, config.ORDER_JPY)
    range_ = _simulate_long(candles, config.RANGE_SL_PCT,
                            config.RANGE_TP_PCT, config.ORDER_JPY)
    if trend is None and range_ is None:
        return None

    return {
        "note": "9時成行ロングの単純ベンチマーク(手数料除く)",
        "trend_params": trend,
        "range_params": range_,
    }


def _cf_log_path() -> str:
    return os.path.join(config.JOURNAL_DIR, "counterfactual.jsonl")


def _append_cf_log(date: str, cf: dict) -> None:
    os.makedirs(config.JOURNAL_DIR, exist_ok=True)
    with open(_cf_log_path(), "a", encoding="utf-8") as f:
        f.write(json.dumps({"date": date, **cf}, ensure_ascii=False) + "\n")


def _load_cf_logs(start: str, end: str) -> list[dict]:
    """start〜end(両端含む, YYYY-MM-DD)のcounterfactual記録を読む。
    同一日付が重複していたら最後の記録を採用する"""
    path = _cf_log_path()
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
            except json.JSONDecodeError:
                continue
            d = rec.get("date", "")
            if start <= d <= end:
                by_date[d] = rec
    return [by_date[d] for d in sorted(by_date)]


# ---------------------------------------------------------------
# 日誌
# ---------------------------------------------------------------

def write_journal(date: str | None = None) -> str:
    date = date or _target_date()
    plan = db.load_plan(date) or {}
    trades = db.trades_for(date)
    total = sum(t["pl_jpy"] for t in trades)

    # no_trade日かつ実トレードゼロの日のみ仮想検証を行う
    # ※planのキー名が"regime"でない場合はここを実際のキー名に合わせること
    cf = None
    if plan.get("regime") == "no_trade" and not trades:
        cf = counterfactual_for(date)
        if cf:
            _append_cf_log(date, cf)

    if cf:
        cf_text = json.dumps(cf, ensure_ascii=False, indent=1)
    elif plan.get("regime") == "no_trade" and not trades:
        cf_text = "(データ取得失敗のため計算できず)"
    else:
        cf_text = "(no_trade日ではないため対象外)"

    resp = _client().messages.create(
        model=config.CLAUDE_MODEL,
        max_tokens=config.JOURNAL_MAX_TOKENS,
        system=JOURNAL_SYSTEM,
        messages=[{"role": "user", "content":
                   f"日付: {date}\n\n朝の作戦:\n"
                   f"{json.dumps(plan, ensure_ascii=False, indent=1)}\n\n"
                   f"トレード一覧({len(trades)}件, 合計{total:+.0f}円):\n"
                   f"{json.dumps(trades, ensure_ascii=False, indent=1)}\n\n"
                   f"仮想トレード結果(no_trade日のみ):\n{cf_text}"}],
    )
    text = "\n".join(b.text for b in resp.content if b.type == "text")

    os.makedirs(config.JOURNAL_DIR, exist_ok=True)
    path = os.path.join(config.JOURNAL_DIR, f"{date}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)

    notify.send(f"📓 **日誌 {date}** ({len(trades)}トレード, {total:+.0f}円)\n"
                + text[:800])
    return path


# ---------------------------------------------------------------
# 週次反省会
# ---------------------------------------------------------------

def weekly_review() -> str:
    # 前日を基準にその週(月〜)を対象にする。
    # 月曜朝のcron実行なら「前週の月曜〜日曜」が正しく対象になる
    ref = datetime.now(JST) - timedelta(days=1)
    end = ref.strftime("%Y-%m-%d")
    monday = (ref - timedelta(days=ref.weekday())).strftime("%Y-%m-%d")

    journals = []
    for p in sorted(glob.glob(os.path.join(config.JOURNAL_DIR, "*.md"))):
        name = os.path.basename(p)[:10]
        if monday <= name <= end:
            with open(p, encoding="utf-8") as f:
                journals.append(f.read())

    week_pl = db.pl_since_between(monday, end)
    current_params = {k: getattr(config, k) for k in
                      ("TREND_SL_PCT", "TREND_TP_PCT", "RANGE_SL_PCT",
                       "RANGE_TP_PCT", "RSI_OVERSOLD", "EMA_NEAR_PCT",
                       "ORDER_JPY")}

    # 週間の仮想トレード集計(no_trade判定の実額評価)
    cf_logs = _load_cf_logs(monday, end)
    if cf_logs:
        trend_sum = sum(r["trend_params"]["pl_jpy"] for r in cf_logs
                        if r.get("trend_params"))
        range_sum = sum(r["range_params"]["pl_jpy"] for r in cf_logs
                        if r.get("range_params"))
        cf_summary = (
            f"no_trade日数: {len(cf_logs)}日\n"
            f"トレンド用パラメータで仮に取引した場合の合計: {trend_sum:+.0f}円\n"
            f"レンジ用パラメータで仮に取引した場合の合計: {range_sum:+.0f}円\n"
            f"(マイナスなら「回避できた損失」、プラスなら「機会損失」)\n"
            f"日別詳細:\n"
            + json.dumps(cf_logs, ensure_ascii=False, indent=1))
    else:
        cf_summary = "(今週の仮想トレード記録なし)"

    resp = _client().messages.create(
        model=config.CLAUDE_MODEL,
        max_tokens=config.REVIEW_MAX_TOKENS,
        system=REVIEW_SYSTEM,
        messages=[{"role": "user", "content":
                   f"対象週: {monday}〜{end} / 週間損益 {week_pl:+.0f}円\n"
                   f"現在のパラメータ: {json.dumps(current_params)}\n\n"
                   f"仮想トレード集計:\n{cf_summary}\n\n"
                   "日誌:\n\n" + "\n\n---\n\n".join(journals)}],
    )
    text = "\n".join(b.text for b in resp.content if b.type == "text")

    path = os.path.join(config.JOURNAL_DIR, f"review-{monday}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    notify.send(f"🧭 **週次反省会** ({monday}週, {week_pl:+.0f}円)\n"
                + text[:1200])
    return path
