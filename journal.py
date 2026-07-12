# -*- coding: utf-8 -*-
"""
夜の日誌 & 週次反省会
=====================
- journal: 当日の作戦とトレードをClaudeが振り返りMarkdown日誌に記録
- review : 直近1週間の日誌と成績からパラメータ変更を「提案」する。
           自動では何も変えない。人間がconfig.pyを書き換えて初めて反映。
"""
import glob
import json
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import anthropic

import config
import db
import notify

JST = ZoneInfo("Asia/Tokyo")

JOURNAL_SYSTEM = """あなたはBTC/JPY現物自動売買botのトレード日誌を書く担当です。
渡された「朝の作戦」と「当日の全トレード」を突き合わせて、以下の構成の
Markdown日誌を日本語で書いてください。感想ではなく検証を書くこと。

# YYYY-MM-DD トレード日誌
## 本日の成績
## 朝のレジーム判定は当たったか
## ルール通りに執行できたか
## 気づき(次に活かす具体的な1点)
## 📘 今日の用語解説
本文で使った専門用語や相場概念を1つ選び、投資初心者向けに
たとえ話を交えて3文以内で解説する。

全体で500字以内。誇張せず、負けた日は負けた事実を淡々と分析すること。"""

REVIEW_SYSTEM = """あなたはBTC/JPY現物自動売買botの週次レビュー担当です。
1週間分の日誌と成績データから、以下をMarkdownで出力してください。

# 週次反省会 (WEEK)
## 今週の総括(勝率・平均損益・レジーム判定の的中率)
## うまくいったパターン / いかなかったパターン
## パラメータ変更の提案(最大2つまで)
提案は「変更対象のconfig.py変数名 / 現在値 / 提案値 / 根拠」の形式で。
根拠がデータで示せない変更は提案しないこと。変更ゼロという結論も歓迎。
最後に必ず「※この提案は自動反映されません。納得した場合のみconfig.pyを
手動で変更してください」と書くこと。"""


def _client():
    return anthropic.Anthropic()


def _target_date() -> str:
    """深夜(6時前)の実行は前日分を対象にする(cron 0:05運用のため)"""
    now = datetime.now(JST)
    if now.hour < 6:
        now -= timedelta(days=1)
    return now.strftime("%Y-%m-%d")


def write_journal(date: str | None = None) -> str:
    date = date or _target_date()
    plan = db.load_plan(date) or {}
    trades = db.trades_for(date)
    total = sum(t["pl_jpy"] for t in trades)

    resp = _client().messages.create(
        model=config.CLAUDE_MODEL,
        max_tokens=config.JOURNAL_MAX_TOKENS,
        system=JOURNAL_SYSTEM,
        messages=[{"role": "user", "content":
                   f"日付: {date}\n\n朝の作戦:\n"
                   f"{json.dumps(plan, ensure_ascii=False, indent=1)}\n\n"
                   f"トレード一覧({len(trades)}件, 合計{total:+.0f}円):\n"
                   f"{json.dumps(trades, ensure_ascii=False, indent=1)}"}],
    )
    text = "\n".join(b.text for b in resp.content if b.type == "text")

    os.makedirs(config.JOURNAL_DIR, exist_ok=True)
    path = os.path.join(config.JOURNAL_DIR, f"{date}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)

    notify.send(f"📓 **日誌 {date}** ({len(trades)}トレード, {total:+.0f}円)\n"
                + text[:800])
    return path


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

    resp = _client().messages.create(
        model=config.CLAUDE_MODEL,
        max_tokens=config.REVIEW_MAX_TOKENS,
        system=REVIEW_SYSTEM,
        messages=[{"role": "user", "content":
                   f"対象週: {monday}〜{end} / 週間損益 {week_pl:+.0f}円\n"
                   f"現在のパラメータ: {json.dumps(current_params)}\n\n"
                   "日誌:\n\n" + "\n\n---\n\n".join(journals)}],
    )
    text = "\n".join(b.text for b in resp.content if b.type == "text")

    path = os.path.join(config.JOURNAL_DIR, f"review-{monday}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    notify.send(f"🧭 **週次反省会** ({monday}週, {week_pl:+.0f}円)\n"
                + text[:1200])
    return path
