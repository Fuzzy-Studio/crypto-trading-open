#!/bin/bash

# 网格波动率扫描器启动脚本 - Binance Top 50

cd "$(dirname "$0")/.."

echo "========================================================================"
echo "📊 网格波动率扫描器 - Binance Top 50"
echo "========================================================================"
echo "启动中..."
echo ""

# 使用 Binance Top 50 配置文件
python3 grid_volatility_scanner/run_scanner.py --exchange binance --config grid_volatility_scanner/config/binance_top50_config.yaml
