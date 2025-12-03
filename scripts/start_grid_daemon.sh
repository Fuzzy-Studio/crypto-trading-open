#!/bin/bash

###############################################################################
# 网格交易后台启动脚本
# 
# 功能：
#   - 启动网格交易系统（后台模式）
#   - 支持 nohup 或 screen 方式运行
#   - 自动记录 PID 用于停止
#   - 日志重定向到指定文件
#
# 用法：
#   ./scripts/start_grid_daemon.sh <config_file> [--screen]
#
# 示例：
#   # 使用 nohup 后台运行（默认）
#   ./scripts/start_grid_daemon.sh config/grid/lighter_btc_perp_long.yaml
#
#   # 使用 screen 会话运行
#   ./scripts/start_grid_daemon.sh config/grid/lighter_btc_perp_long.yaml --screen
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
SCRIPT_NAME="run_grid_trading_daemon.py"
PID_DIR="$PROJECT_ROOT/pids"
LOG_DIR="$PROJECT_ROOT/logs"

# 创建必要目录
mkdir -p "$PID_DIR"
mkdir -p "$LOG_DIR"

# 检查参数
if [ $# -lt 1 ]; then
    echo -e "${RED}❌ 错误: 缺少配置文件参数${NC}"
    echo ""
    echo "用法: $0 <config_file> [--screen]"
    echo ""
    echo "示例:"
    echo "  $0 config/grid/lighter_btc_perp_long.yaml"
    echo "  $0 config/grid/lighter_btc_perp_long.yaml --screen"
    exit 1
fi

CONFIG_FILE="$1"
USE_SCREEN=false

# 检查是否使用 screen
if [ "$2" = "--screen" ]; then
    USE_SCREEN=true
fi

# 检查配置文件
if [ ! -f "$CONFIG_FILE" ]; then
    echo -e "${RED}❌ 配置文件不存在: $CONFIG_FILE${NC}"
    exit 1
fi

# 从配置文件生成唯一标识
CONFIG_BASENAME=$(basename "$CONFIG_FILE" .yaml)
PID_FILE="$PID_DIR/grid_${CONFIG_BASENAME}.pid"
LOG_FILE="$LOG_DIR/grid_${CONFIG_BASENAME}.log"
SCREEN_NAME="grid_${CONFIG_BASENAME}"

# 检查是否已在运行
if [ -f "$PID_FILE" ]; then
    OLD_PID=$(cat "$PID_FILE")
    if ps -p "$OLD_PID" > /dev/null 2>&1; then
        echo -e "${YELLOW}⚠️  网格交易已在运行 (PID: $OLD_PID)${NC}"
        echo -e "${YELLOW}   配置: $CONFIG_FILE${NC}"
        echo ""
        echo "如需重启，请先运行停止脚本:"
        echo "  ./scripts/stop_grid_daemon.sh $CONFIG_FILE"
        exit 1
    else
        echo -e "${YELLOW}⚠️  发现过期的 PID 文件，清理中...${NC}"
        rm -f "$PID_FILE"
    fi
fi

echo -e "${BLUE}═══════════════════════════════════════════════════════════${NC}"
echo -e "${BLUE}  网格交易系统 - 后台启动${NC}"
echo -e "${BLUE}═══════════════════════════════════════════════════════════${NC}"
echo ""
echo -e "${GREEN}📋 配置文件:${NC} $CONFIG_FILE"
echo -e "${GREEN}📝 日志文件:${NC} $LOG_FILE"
echo -e "${GREEN}🔢 PID文件:${NC} $PID_FILE"

# 检查 Python
if ! command -v python3 &> /dev/null; then
    echo -e "${RED}❌ 错误: 未找到 python3${NC}"
    exit 1
fi

# 启动网格系统
if [ "$USE_SCREEN" = true ]; then
    # 使用 screen 方式
    echo -e "${GREEN}🚀 启动模式:${NC} screen (会话名: $SCREEN_NAME)"
    echo ""
    
    # 检查 screen 是否安装
    if ! command -v screen &> /dev/null; then
        echo -e "${RED}❌ 错误: 未找到 screen 命令${NC}"
        echo "   请安装 screen: sudo apt-get install screen 或 brew install screen"
        exit 1
    fi
    
    # 检查是否已存在同名 screen 会话
    if screen -list | grep -q "$SCREEN_NAME"; then
        echo -e "${YELLOW}⚠️  发现同名 screen 会话，正在清理...${NC}"
        screen -S "$SCREEN_NAME" -X quit 2>/dev/null || true
        sleep 1
    fi
    
    # 在 screen 中启动
    screen -dmS "$SCREEN_NAME" bash -c "cd '$PROJECT_ROOT' && python3 $SCRIPT_NAME '$CONFIG_FILE' 2>&1 | tee '$LOG_FILE'"
    sleep 2
    
    # 获取 PID（从 screen 会话中的进程）
    GRID_PID=$(pgrep -f "python3 $SCRIPT_NAME $CONFIG_FILE" || echo "")
    
    if [ -z "$GRID_PID" ]; then
        echo -e "${RED}❌ 启动失败，未找到进程${NC}"
        exit 1
    fi
    
    echo "$GRID_PID" > "$PID_FILE"
    
    echo -e "${GREEN}✅ 网格交易系统已启动${NC}"
    echo -e "${GREEN}   PID: $GRID_PID${NC}"
    echo -e "${GREEN}   Screen 会话: $SCREEN_NAME${NC}"
    echo ""
    echo "管理命令:"
    echo -e "  ${BLUE}查看日志:${NC} screen -r $SCREEN_NAME"
    echo -e "  ${BLUE}分离会话:${NC} Ctrl+A, D"
    echo -e "  ${BLUE}停止系统:${NC} ./scripts/stop_grid_daemon.sh $CONFIG_FILE"
    
else
    # 使用 nohup 方式（默认）
    echo -e "${GREEN}🚀 启动模式:${NC} nohup (后台守护进程)"
    echo ""
    
    # 启动进程
    nohup python3 "$SCRIPT_NAME" "$CONFIG_FILE" > "$LOG_FILE" 2>&1 &
    GRID_PID=$!
    
    # 等待进程启动
    sleep 2
    
    # 验证进程是否还在运行
    if ! ps -p "$GRID_PID" > /dev/null 2>&1; then
        echo -e "${RED}❌ 启动失败${NC}"
        echo ""
        echo "最后50行日志:"
        tail -n 50 "$LOG_FILE"
        exit 1
    fi
    
    # 保存 PID
    echo "$GRID_PID" > "$PID_FILE"
    
    echo -e "${GREEN}✅ 网格交易系统已启动${NC}"
    echo -e "${GREEN}   PID: $GRID_PID${NC}"
    echo ""
    echo "管理命令:"
    echo -e "  ${BLUE}查看日志:${NC} tail -f $LOG_FILE"
    echo -e "  ${BLUE}查看进程:${NC} ps -p $GRID_PID"
    echo -e "  ${BLUE}停止系统:${NC} ./scripts/stop_grid_daemon.sh $CONFIG_FILE"
fi

echo ""
echo -e "${BLUE}═══════════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}🎉 启动完成！${NC}"
echo -e "${BLUE}═══════════════════════════════════════════════════════════${NC}"
echo ""

# 显示最后几行日志
echo "最后10行日志:"
echo "─────────────────────────────────────────────────────────"
tail -n 10 "$LOG_FILE" 2>/dev/null || echo "暂无日志"
echo "─────────────────────────────────────────────────────────"
