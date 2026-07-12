# -*- coding: utf-8 -*-
"""
朝の作戦会議(レジーム判定)— BTC/JPY版
========================================
毎朝7時にClaudeが担当する唯一の裁量部分。
チャートデータはbitbank Public API(口座不要)から取得。
"""
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import anthropic

import config
import db
import market_data
import notify
from indicators import summarize_market

JST = ZoneInfo("Asia/Tokyo")

SYSTEM_PROMPT = """あなたはBTC/JPY(ビットコイン円)の現物デイトレード戦略を統括する
アナリストです。毎朝、その日の「レジーム(相場環境)」だけを判定します。
実際の売買は機械的なルールエンジンが行い、現物のため買いエントリーしか
できません。下落を予想した日は「見送り」が正解になります。

判定にあたって、web検索で以下を必ず確認してください:
1. 直近24時間の暗号資産市場の大きなニュース(規制、ETF、ハッキング、
   大口清算、取引所障害など)
2. 本日(日本時間)のビットコイン価格に影響しうる予定(FOMC、米CPI、
   大型オプションSQ、主要アップデートなど)

出力は次のJSONのみ。前置き・後書き・コードブロック記号は一切禁止:
{
  "regime": "trend_up" | "trend_down" | "range" | "no_trade",
  "confidence": 0.0-1.0,
  "caution_windows": [["HH:MM", "HH:MM"], ...],
  "summary": "判定理由を日本語で3文以内",
  "key_events": ["21:30 米CPI", ...],
  "learn": "summaryで使った専門用語を1つ選び、投資初心者向けに2文で解説"
}

判定基準:
- trend_down は「買わない」という意味を持つ(現物のため空売りはしない)
- 重要イベント当日は発表前後60分を caution_windows に含める。
  イベントが支配的な日は no_trade を選ぶ
- 迷ったら no_trade。confidence が 0.6 未満なら no_trade を推奨する
- 暗号資産は週末も動くが、週末は流動性が薄く値が飛びやすいことを考慮する"""


def build_market_summary() -> dict:
    k15 = market_data.recent_candles("15min", days=2)
    k1h = market_data.recent_candles("1hour", days=3)
    return summarize_market(k15, k1h)


def run_morning_briefing() -> dict:
    market = build_market_summary()
    today = datetime.now(JST).strftime("%Y年%m月%d日(%a)")

    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=config.CLAUDE_MODEL,
        max_tokens=config.MORNING_MAX_TOKENS,
        system=SYSTEM_PROMPT,
        tools=[{"type": "web_search_20250305", "name": "web_search",
                "max_uses": 4}],
        messages=[{"role": "user", "content":
                   f"本日は {today} です。BTC/JPYの市場サマリー:\n"
                   f"{json.dumps(market, ensure_ascii=False, indent=1)}\n\n"
                   "web検索で暗号資産市場の最新状況を確認し、"
                   "指定のJSON形式でレジーム判定を出力してください。"}],
    )

    text = "\n".join(b.text for b in resp.content if b.type == "text")
    text = text.replace("```json", "").replace("```", "").strip()
    start, end = text.find("{"), text.rfind("}")
    plan = json.loads(text[start:end + 1])

    if float(plan.get("confidence", 0)) < 0.6:
        plan["regime"] = "no_trade"

    plan["market_snapshot"] = market
    db.save_plan(plan)

    notify.send(
        f"🌅 **本日の作戦** ({db.today_jst()})\n"
        f"レジーム: **{plan['regime']}** (確度 {plan.get('confidence')})\n"
        f"{plan.get('summary', '')}\n"
        f"警戒時間帯: {plan.get('caution_windows') or 'なし'}\n"
        f"注目イベント: {', '.join(plan.get('key_events', [])) or 'なし'}\n"
        f"📘 今日の豆知識: {plan.get('learn', '')}")
    return plan


if __name__ == "__main__":
    print(json.dumps(run_morning_briefing(), ensure_ascii=False, indent=2))
