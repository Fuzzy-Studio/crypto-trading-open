#!/bin/bash

###############################################################################
# 网格交易后台停止脚本
# 
# 功能：
#   - 安全停止网格交易系统
#   - 发送 SIGTERM 信号允许优雅退出
#   - 自动清理 PID 文件
#   - 支持强制停止
#
# 用法：
#   ./scripts/stop_grid_daemon.sh <config_file> [--force]
#
# 示例：
#   # 优雅停止
#   ./scripts/stop_grid_daemon.sh config/grid/lighter_btc_perp_long.yaml
#
#   # 强制停止
#   ./scripts/stop_grid_daemon.sh config/grid/lighter_btc_perp_long.yaml --force
###############################################################################

set -e

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# 项目根目录
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

# 配置
PID_DIR="$PROJECT_ROOT/pids"
LOG_DIR="$PROJECT_ROOT/logs"

# 检查参数
if [ $# -lt 1 ]; then
    echo -e "${RED}❌ 错误: 缺少配置文件参数${NC}"
    echo ""
    echo "用法: $0 <config_file> [--force]"
    echo ""
    echo "示例:"
    echo "  $0 config/grid/lighter_btc_perp_long.yaml"
    echo "  $0 config/grid/lighter_btc_perp_long.yaml --force"
    exit 1
fi

CONFIG_FILE="$1"
FORCE_STOP=false

# 检查是否强制停止
if [ "$2" = "--force" ]; then
    FORCE_STOP=true
fi

# 从配置文件生成唯一标识
CONFIG_BASENAME=$(basename "$CONFIG_FILE" .yaml)
PID_FILE="$PID_DIR/grid_${CONFIG_BASENAME}.pid"
LOG_FILE="$LOG_DIR/grid_${CONFIG_BASENAME}.log"
SCREEN_NAME="grid_${CONFIG_BASENAME}"

echo -e "${BLUE}═══════════════════════════════════════════════════════════${NC}"
echo -e "${BLUE}  网格交易系统 - 停止${NC}"
echo -e "${BLUE}═══════════════════════════════════════════════════════════${NC}"
echo ""

# 检查 PID 文件
if [ ! -f "$PID_FILE" ]; then
    echo -e "${YELLOW}⚠️  未找到 PID 文件: $PID_FILE${NC}"
    echo ""
    
    # 尝试通过进程名查找
    echo "尝试查找运行中的网格进程..."
    GRID_PID=$(pgrep -f "python3.*run_grid_trading_daemon.py.*$CONFIG_BASENAME" || echo "")
    
    if [ -z "$GRID_PID" ]; then
        echo -e "${GREEN}✅ 没有发现运行中的网格进程${NC}"
        
        # 检查 screen 会话
        if command -v screen &> /dev/null && screen -list | grep -q "$SCREEN_NAME"; then
            echo -e "${YELLOW}⚠️  发现 screen 会话: $SCREEN_NAME${NC}"
            echo "正在终止 screen 会话..."
            screen -S "$SCREEN_NAME" -X quit 2>/dev/null || true
            echo -e "${GREEN}✅ Screen 会话已终止${NC}"
        fi
        
        exit 0
    fi
    
    echo -e "${YELLOW}⚠️  发现运行中的进程 (PID: $GRID_PID)${NC}"
    echo ""
else
    GRID_PID=$(cat "$PID_FILE")
    echo -e "${GREEN}📋 配置文件:${NC} $CONFIG_FILE"
    echo -e "${GREEN}🔢 进程 PID:${NC} $GRID_PID"
    echo ""
fi

# 检查进程是否存在
if ! ps -p "$GRID_PID" > /dev/null 2>&1; then
    echo -e "${YELLOW}⚠️  进程不存在 (PID: $GRID_PID)${NC}"
    echo "清理 PID 文件..."
    rm -f "$PID_FILE"
    
    # 检查 screen 会话
    if command -v screen &> /dev/null && screen -list | grep -q "$SCREEN_NAME"; then
        echo -e "${YELLOW}⚠️  发现孤立的 screen 会话，正在清理...${NC}"
        screen -S "$SCREEN_NAME" -X quit 2>/dev/null || true
    fi
    
    echo -e "${GREEN}✅ 清理完成${NC}"
    exit 0
fi

# 停止进程
if [ "$FORCE_STOP" = true ]; then
    echo -e "${YELLOW}⚠️  强制停止模式${NC}"
    echo "发送 SIGKILL 信号..."
    kill -9 "$GRID_PID" 2>/dev/null || true
    echo -e "${GREEN}✅ 已发送强制停止信号${NC}"
else
    echo "发送 SIGTERM 信号（优雅退出）..."
    kill -TERM "$GRID_PID" 2>/dev/null || true
    echo -e "${GREEN}✅ 已发送停止信号${NC}"
    echo ""
    echo "等待进程退出..."
    
    # 等待进程退出（最多30秒）
    WAIT_COUNT=0
    MAX_WAIT=30
    
    while ps -p "$GRID_PID" > /dev/null 2>&1; do
        sleep 1
        WAIT_COUNT=$((WAIT_COUNT + 1))
        
        if [ $WAIT_COUNT -ge $MAX_WAIT ]; then
            echo ""
            echo -e "${YELLOW}⚠️  进程未在规定时间内退出${NC}"
            echo ""
            read -p "是否强制停止? [y/N] " -n 1 -r
            echo
            if [[ $REPLY =~ ^[Yy]$ ]]; then
                echo "强制停止进程..."
                kill -9 "$GRID_PID" 2>/dev/null || true
                sleep 1
            else
                echo "取消操作，进程仍在运行"
                exit 1
            fi
            break
        fi
        
        # 每5秒显示一次进度
        if [ $((WAIT_COUNT % 5)) -eq 0 ]; then
            echo "  等待中... ($WAIT_COUNT/$MAX_WAIT 秒)"
        fi
    done
fi

# 验证进程已停止
if ps -p "$GRID_PID" > /dev/null 2>&1; then
    echo -e "${RED}❌ 进程仍在运行 (PID: $GRID_PID)${NC}"
    echo "请使用以下命令强制停止:"
    echo "  $0 $CONFIG_FILE --force"
    exit 1
fi

# 清理 PID 文件
if [ -f "$PID_FILE" ]; then
    rm -f "$PID_FILE"
    echo -e "${GREEN}✅ PID 文件已清理${NC}"
fi

# 清理 screen 会话
if command -v screen &> /dev/null && screen -list | grep -q "$SCREEN_NAME"; then
    echo "清理 screen 会话..."
    screen -S "$SCREEN_NAME" -X quit 2>/dev/null || true
    echo -e "${GREEN}✅ Screen 会话已清理${NC}"
fi

echo ""
echo -e "${BLUE}═══════════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}🎉 网格交易系统已安全停止${NC}"
echo -e "${BLUE}═══════════════════════════════════════════════════════════${NC}"
echo ""

# 显示最后几行日志
if [ -f "$LOG_FILE" ]; then
    echo "最后10行日志:"
    echo "─────────────────────────────────────────────────────────"
    tail -n 10 "$LOG_FILE"
    echo "─────────────────────────────────────────────────────────"
    echo ""
    echo -e "${BLUE}完整日志:${NC} $LOG_FILE"
fi
