#!/bin/bash
# cryptobot ワンコマンドセットアップ(ConoHaコンソール用)
# 使い方: bash setup.sh
set -e
REPO="https://github.com/takuyaw19880530-star/cryptobot/raw/main"

echo "═══ cryptobot セットアップ開始 ═══"
timedatectl set-timezone Asia/Tokyo

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3-venv unzip > /dev/null

cd /root
echo "── bot本体をダウンロード ──"
wget -q -O cryptobot.zip "$REPO/cryptobot.zip"
unzip -oq cryptobot.zip
cd cryptobot

echo "── Python環境を構築(1〜2分) ──"
python3 -m venv venv
venv/bin/pip install -q -r requirements.txt

cp -n .env.example .env || true

echo ""
echo "── APIキーの設定(対話式・その場で検証) ──"
venv/bin/python setup_keys.py

echo ""
echo "── 接続確認 ──"
venv/bin/python main.py status

echo ""
echo "═══ セットアップ完了 ═══"
echo "次のコマンド:"
echo "  バックテスト   : venv/bin/python main.py backtest 90"
echo "  常駐サービス化 : bash deploy/install_service.sh"
