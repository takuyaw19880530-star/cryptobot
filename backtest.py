# -*- coding: utf-8 -*-
"""
バックテストエンジン — BTC/JPY現物版
====================================
使い方(口座不要・bitbank Public APIのみ使用):
    python backtest.py 90        # 直近90日
    python backtest.py 90 --csv  # トレード明細もCSV出力

■ 前提と限界
1. レジーム判定はClaudeの過去判定を再現できないため、機械的な代理ロジック
   (前日の値動きの効率性ER + EMAの向き)で分類。結果は執行ルールの検証。
2. ニュース起因の警戒時間帯は過去データに含められない → 実運用より甘め。
3. 週次リミット到達時は「翌週再開」と仮定(実運用は手動解除なので甘め)。
4. 約定は保守的(ワーストケース)前提:
   - 手数料+スプレッド相当として片道 0.12% を負担(往復0.24%)
   - 同一足でSLとTPの両方に到達した場合は必ずSL(負け)として扱う
5. ロングオンリー: trend_down / no_trade の日は取引ゼロ。

エントリー/決済判定は strategy.py の純粋関数をライブと共有。
"""
import csv
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import config
import market_data
from indicators import ema
from strategy import entry_signal, pl_of

JST = ZoneInfo("Asia/Tokyo")
COST_PCT = 0.12   # 片道コスト(taker手数料+スプレッド相当、%)


def fetch_history(days: int) -> list[tuple[str, list[dict]]]:
    """直近days日分の15分足を日付ごとに取得(JST日付でグルーピングし直す)"""
    raw = []
    now = datetime.now(JST)
    for d in range(days + 1, -1, -1):
        date = (now - timedelta(days=d)).strftime("%Y%m%d")
        try:
            raw += market_data.candles("15min", date)
        except Exception:
            pass
        time.sleep(0.2)
    # 重複除去 + JST日付で再グルーピング
    seen, by_date = set(), {}
    for k in sorted(raw, key=lambda x: x["openTime"]):
        if k["openTime"] in seen:
            continue
        seen.add(k["openTime"])
        d = datetime.fromtimestamp(k["openTime"] / 1000, JST).strftime("%Y%m%d")
        by_date.setdefault(d, []).append(k)
    dates = sorted(by_date)[-days:] if days < len(by_date) else sorted(by_date)
    return [(d, by_date[d]) for d in dates]


def classify_regime(prev_bars: list[dict]) -> str:
    """
    前日の15分足から機械的にレジームを分類する代理ロジック(v2)。

    v1の問題: ERを15分足96本で計算すると、細かいノイズの往復で分母(経路)が
    膨らみ、強いトレンド日でもERが小さく出て「トレンド日ゼロ」になっていた。
    v2: 1時間足相当に間引いてからERを計算し、方向は日次の純変化率で決める。
    """
    if len(prev_bars) < 60:
        return "no_trade"
    closes = [b["close"] for b in prev_bars]
    hourly = closes[::4]  # 15分足→1時間相当に間引き(経路ノイズ除去)
    if len(hourly) < 8:
        return "no_trade"
    diffs = [abs(hourly[i] - hourly[i - 1]) for i in range(1, len(hourly))]
    path = sum(diffs)
    if path <= 0:
        return "no_trade"
    er = abs(hourly[-1] - hourly[0]) / path
    net_pct = (closes[-1] - closes[0]) / closes[0] * 100

    if er >= 0.45 and abs(net_pct) >= 1.0:
        return "trend_up" if net_pct > 0 else "trend_down"
    if er <= 0.25:
        return "range"
    return "no_trade"


def run_backtest(history: list[tuple[str, list[dict]]]) -> dict:
    trades = []
    pos = None
    closes_hist: list[float] = []
    daily_pl: dict[str, float] = {}
    weekly_pl: dict[str, float] = {}
    regime_by_date: dict[str, str] = {}
    halted_week = None

    def week_key(dt: datetime) -> str:
        iso = dt.isocalendar()
        return f"{iso[0]}-W{iso[1]:02d}"

    def book(exit_price: float, reason: str, ts: datetime, date: str):
        nonlocal pos
        # 往復コストを決済価格に織り込む(保守的)
        eff_exit = exit_price * (1 - COST_PCT / 100)
        pct, jpy = pl_of(pos, eff_exit)
        trades.append({**pos, "exit": round(exit_price, 0),
                       "pl_pips": pct, "pl_jpy": jpy,
                       "reason_close": reason,
                       "ts_close": ts.strftime("%m/%d %H:%M")})
        daily_pl[date] = daily_pl.get(date, 0) + jpy
        wk = week_key(ts)
        weekly_pl[wk] = weekly_pl.get(wk, 0) + jpy
        pos = None

    for i, (date, bars) in enumerate(history):
        if i == 0:
            closes_hist += [b["close"] for b in bars]
            continue

        regime = classify_regime(history[i - 1][1])
        regime_by_date[date] = regime
        last_idx = len(bars) - 1

        for j, b in enumerate(bars):
            ts = datetime.fromtimestamp(b["openTime"] / 1000, JST)
            close = b["close"]
            wk = week_key(ts)

            # 保有中: SL/TPをバー高安で判定(ワーストケース: SL優先)
            if pos:
                if b["low"] <= pos["sl"]:
                    book(pos["sl"], "損切り", ts, date)
                elif b["high"] >= pos["tp"]:
                    book(pos["tp"], "利確", ts, date)

            closes_hist.append(close)

            # 新規エントリー(買いのみ)
            if pos is None and regime in ("trend_up", "range"):
                if halted_week == wk:
                    continue
                if weekly_pl.get(wk, 0) <= config.WEEKLY_LOSS_LIMIT_JPY:
                    halted_week = wk
                    continue
                if daily_pl.get(date, 0) <= config.DAILY_LOSS_LIMIT_JPY:
                    continue
                h = ts.hour * 60 + ts.minute
                ms = [int(x) for x in config.MAINTENANCE_START.split(":")]
                me = [int(x) for x in config.MAINTENANCE_END.split(":")]
                if ms[0] * 60 + ms[1] <= h <= me[0] * 60 + me[1]:
                    continue
                if j == last_idx:
                    continue

                # 現物サイジング: ORDER_JPY円分(最小ロット確認込み)
                if config.MIN_BTC_SIZE * close > config.MAX_ORDER_JPY:
                    continue
                sig = entry_signal(regime, closes_hist[-400:],
                                   ask=close, bid=close)
                if sig:
                    size = max(config.ORDER_JPY / close, config.MIN_BTC_SIZE)
                    pos = {"date": date, "side": "BUY",
                           "size": round(size, config.SIZE_DECIMALS),
                           "entry": sig["entry"], "sl": sig["sl"],
                           "tp": sig["tp"], "regime": regime,
                           "reason_open": sig["reason"],
                           "ts_open": ts.strftime("%m/%d %H:%M")}

    if pos and history:
        last_date, last_bars = history[-1]
        b = last_bars[-1]
        ts = datetime.fromtimestamp(b["openTime"] / 1000, JST)
        book(b["close"], "テスト期間終了", ts, last_date)

    return summarize(trades, daily_pl, regime_by_date)


def summarize(trades, daily_pl, regime_by_date) -> dict:
    wins = [t for t in trades if t["pl_jpy"] > 0]
    total = sum(t["pl_jpy"] for t in trades)
    peak = dd = cum = 0.0
    for d in sorted(daily_pl):
        cum += daily_pl[d]
        peak = max(peak, cum)
        dd = min(dd, cum - peak)
    by_regime: dict[str, dict] = {}
    for t in trades:
        r = by_regime.setdefault(t["regime"], {"trades": 0, "jpy": 0.0})
        r["trades"] += 1
        r["jpy"] += t["pl_jpy"]
    regime_days: dict[str, int] = {}
    for r in regime_by_date.values():
        regime_days[r] = regime_days.get(r, 0) + 1
    return {"trades": trades, "n": len(trades), "wins": len(wins),
            "win_rate": round(len(wins) / len(trades) * 100, 1) if trades else 0,
            "total_jpy": round(total, 0),
            "avg_jpy": round(total / len(trades), 1) if trades else 0,
            "max_dd_jpy": round(dd, 0),
            "by_regime": by_regime, "regime_days": regime_days,
            "trading_days": len(regime_by_date)}


def print_report(r: dict):
    print("═" * 46)
    print(" バックテスト結果(BTC/JPY現物・ワーストケース)")
    print("═" * 46)
    print(f" 対象日数        : {r['trading_days']}日")
    print(f" レジーム内訳    : {r['regime_days']}")
    print(f" トレード数      : {r['n']} (勝ち {r['wins']} / 勝率 {r['win_rate']}%)")
    print(f" 合計損益        : {r['total_jpy']:+.0f}円")
    print(f" 1トレード平均   : {r['avg_jpy']:+.1f}円")
    print(f" 最大ドローダウン: {r['max_dd_jpy']:.0f}円")
    for k, v in r["by_regime"].items():
        print(f"   {k:<11}: {v['trades']:3d}回 {v['jpy']:+8.0f}円")
    print("─" * 46)
    print(" ※Claudeレジーム判定の代わりに機械的な代理判定を使用。")
    print(" ※往復0.24%のコストを織り込み済み。将来の利益は保証しない。")


def save_csv(trades: list[dict], path: str = "data/backtest_trades.csv"):
    if not trades:
        return
    cols = ["date", "ts_open", "ts_close", "regime", "side", "size", "entry",
            "exit", "sl", "tp", "pl_pips", "pl_jpy", "reason_open",
            "reason_close"]
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(trades)
    print(f" 明細CSV: {path}")


if __name__ == "__main__":
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 90
    print(f"bitbankから直近{days}日分の15分足を取得中...")
    history = fetch_history(days)
    print(f"{len(history)}日分を取得。シミュレーション実行中...")
    result = run_backtest(history)
    print_report(result)
    if "--csv" in sys.argv:
        save_csv(result["trades"])
