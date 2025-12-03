#!/usr/bin/env python3
"""
网格交易系统后台运行脚本

适合云主机部署的后台运行模式，无需终端 UI

⚠️ 【重要】Lighter 交易所用户必读：
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
由于 Lighter SDK (v0.1.4) 的底层 C 库存在 Bug，脚本无法自动设置逐仓（isolated）模式。

🔧 解决方案：
1. 登录 Lighter 交易所网站 (https://app.lighter.xyz)
2. 手动为每个交易对设置保证金模式和杠杆倍数
3. 设置完成后，运行本脚本即可正常交易

📝 建议配置：
   - 主流币（BTC/ETH）：Cross 或 Isolated，10-20倍杠杆
   - 小市值币（MEGA/VIRTUAL）：Cross 或 Isolated，3-5倍杠杆
   - 做空操作：建议至少 3 倍杠杆

💡 提示：
   - 配置文件中的 margin_mode 和 leverage 仅作为参考，不会自动设置
   - 网页端设置一次后，脚本会使用该设置，无需每次重复设置
   - 详细说明见：docs/fixes/lighter_margin_mode_sdk_bug.md
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

from core.adapters.exchanges.utils import setup_optimized_logging
from core.adapters.exchanges.models import ExchangeType
from core.adapters.exchanges import ExchangeFactory, ExchangeConfig
from core.services.grid.coordinator import GridCoordinator
from core.services.grid.implementations import (
    GridStrategyImpl,
    GridEngineImpl,
    PositionTrackerImpl
)
from core.services.grid.models import GridConfig, GridType, GridState
from core.services.grid.reserve import (
    SpotReserveManager,
    ReserveMonitor,
    check_spot_reserve_on_startup
)
from core.logging import get_system_logger
import sys
import asyncio
import yaml
from pathlib import Path
from decimal import Decimal
import argparse
import logging
import signal
import os
from datetime import datetime

# 添加项目根目录到路径
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))


class DaemonGridRunner:
    """后台运行的网格交易管理器"""

    def __init__(self, config_path: str, debug: bool = False):
        self.config_path = config_path
        self.debug = debug
        self.logger = get_system_logger()
        
        # 运行状态
        self._running = False
        self._shutdown_event = asyncio.Event()
        
        # 核心组件
        self.coordinator = None
        self.exchange_adapter = None
        self.reserve_monitor = None
        
        # 统计信息刷新间隔（秒）
        self.stats_interval = 300  # 每5分钟输出一次统计
        
    async def load_config(self) -> dict:
        """加载配置文件"""
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f)
            return config
        except Exception as e:
            self.logger.error(f"❌ 加载配置文件失败: {e}")
            raise

    def create_grid_config(self, config_data: dict) -> GridConfig:
        """创建网格配置对象（复用原脚本的逻辑）"""
        grid_config = config_data['grid_system']
        grid_type = GridType(grid_config['grid_type'])

        # 基础参数
        params = {
            'exchange': grid_config['exchange'],
            'symbol': grid_config['symbol'],
            'grid_type': grid_type,
            'grid_interval': Decimal(str(grid_config['grid_interval'])),
            'order_amount': Decimal(str(grid_config['order_amount'])),
            'max_position': Decimal(str(grid_config.get('max_position'))) if grid_config.get('max_position') else None,
            'enable_notifications': grid_config.get('enable_notifications', False),
            'order_health_check_enabled': grid_config.get('order_health_check_enabled', True),
            'order_health_check_interval': grid_config.get('order_health_check_interval', 600),
            'rest_position_query_interval': grid_config.get('rest_position_query_interval', 1),
            'fee_rate': Decimal(str(grid_config.get('fee_rate', '0.0001'))),
            'quantity_precision': int(grid_config.get('quantity_precision', 3)),
            'price_decimals': int(grid_config.get('price_decimals', 2)),
        }

        # 价格移动网格参数
        if grid_type in [GridType.FOLLOW_LONG, GridType.FOLLOW_SHORT]:
            params['follow_grid_count'] = grid_config['follow_grid_count']
            params['follow_timeout'] = grid_config.get('follow_timeout', 300)
            params['follow_distance'] = grid_config.get('follow_distance', 1)
            params['price_offset_grids'] = grid_config.get('price_offset_grids', 0)
        else:
            params['lower_price'] = Decimal(str(grid_config['price_range']['lower_price']))
            params['upper_price'] = Decimal(str(grid_config['price_range']['upper_price']))

        # 马丁网格参数
        if 'martingale_increment' in grid_config:
            params['martingale_increment'] = Decimal(str(grid_config['martingale_increment']))

        # 剥头皮模式参数
        if 'scalping_enabled' in grid_config:
            params['scalping_enabled'] = grid_config['scalping_enabled']
        if 'scalping_trigger_percent' in grid_config:
            params['scalping_trigger_percent'] = grid_config['scalping_trigger_percent']
        if 'scalping_take_profit_grids' in grid_config:
            params['scalping_take_profit_grids'] = grid_config['scalping_take_profit_grids']

        # 智能剥头皮模式参数
        if 'smart_scalping_enabled' in grid_config:
            params['smart_scalping_enabled'] = grid_config['smart_scalping_enabled']
        if 'allowed_deep_drops' in grid_config:
            params['allowed_deep_drops'] = grid_config['allowed_deep_drops']
        if 'min_drop_threshold_percent' in grid_config:
            params['min_drop_threshold_percent'] = grid_config['min_drop_threshold_percent']

        # 本金保护模式参数
        if 'capital_protection_enabled' in grid_config:
            params['capital_protection_enabled'] = grid_config['capital_protection_enabled']
        if 'capital_protection_trigger_percent' in grid_config:
            params['capital_protection_trigger_percent'] = grid_config['capital_protection_trigger_percent']

        # 止盈模式参数
        if 'take_profit_enabled' in grid_config:
            params['take_profit_enabled'] = grid_config['take_profit_enabled']
        if 'take_profit_percentage' in grid_config:
            params['take_profit_percentage'] = Decimal(str(grid_config['take_profit_percentage']))

        # 价格锁定模式参数
        if 'price_lock_enabled' in grid_config:
            params['price_lock_enabled'] = grid_config['price_lock_enabled']
        if 'price_lock_threshold' in grid_config:
            params['price_lock_threshold'] = Decimal(str(grid_config['price_lock_threshold']))
        if 'price_lock_start_at_threshold' in grid_config:
            params['price_lock_start_at_threshold'] = grid_config['price_lock_start_at_threshold']

        # 反手挂单参数
        if 'reverse_order_grid_distance' in grid_config:
            params['reverse_order_grid_distance'] = int(grid_config['reverse_order_grid_distance'])

        # 止损保护模式参数
        if 'stop_loss_protection_enabled' in grid_config:
            params['stop_loss_protection_enabled'] = grid_config['stop_loss_protection_enabled']
        if 'stop_loss_trigger_percent' in grid_config:
            params['stop_loss_trigger_percent'] = Decimal(str(grid_config['stop_loss_trigger_percent']))
        if 'stop_loss_escape_timeout' in grid_config:
            params['stop_loss_escape_timeout'] = int(grid_config['stop_loss_escape_timeout'])
        if 'stop_loss_apr_threshold' in grid_config:
            params['stop_loss_apr_threshold'] = Decimal(str(grid_config['stop_loss_apr_threshold']))

        # 退出清理模式参数
        if 'exit_cleanup_enabled' in grid_config:
            params['exit_cleanup_enabled'] = grid_config['exit_cleanup_enabled']

        # 保证金模式参数
        if 'margin_mode' in grid_config:
            params['margin_mode'] = str(grid_config['margin_mode'])

        # 杠杆倍数参数
        if 'leverage' in grid_config:
            params['leverage'] = int(grid_config['leverage'])

        # 现货预留管理配置
        if 'spot_reserve' in grid_config:
            params['spot_reserve'] = grid_config['spot_reserve']

        # 健康检查容错配置
        if 'position_tolerance' in grid_config:
            params['position_tolerance'] = grid_config['position_tolerance']

        # 健康检查快照次数配置
        if 'health_check_snapshot_count' in grid_config:
            params['health_check_snapshot_count'] = int(grid_config['health_check_snapshot_count'])

        return GridConfig(**params)

    def detect_market_type(self, symbol: str, exchange_name: str) -> ExchangeType:
        """检测市场类型（复用原脚本的逻辑）"""
        symbol_upper = symbol.upper()
        exchange_lower = exchange_name.lower()

        if exchange_lower == "hyperliquid":
            if ":USDC" in symbol_upper or ":PERP" in symbol_upper or ":SPOT" in symbol_upper:
                if ":SPOT" in symbol_upper:
                    return ExchangeType.SPOT
                else:
                    return ExchangeType.PERPETUAL
            else:
                return ExchangeType.SPOT
        elif exchange_lower == "backpack":
            if "_PERP" in symbol_upper or "PERP" in symbol_upper:
                return ExchangeType.PERPETUAL
            elif "_SPOT" in symbol_upper or "SPOT" in symbol_upper:
                return ExchangeType.SPOT
            else:
                return ExchangeType.PERPETUAL
        elif exchange_lower == "lighter":
            return ExchangeType.PERPETUAL
        else:
            return ExchangeType.PERPETUAL

    async def create_exchange_adapter(self, config_data: dict):
        """创建交易所适配器（复用原脚本的逻辑）"""
        grid_config = config_data['grid_system']
        exchange_name = grid_config['exchange'].lower()
        symbol = grid_config['symbol']
        market_type = self.detect_market_type(symbol, exchange_name)

        self.logger.info(f"市场类型: {market_type.value}")

        # 读取 API 密钥
        api_key = os.getenv(f"{exchange_name.upper()}_API_KEY")
        api_secret = os.getenv(f"{exchange_name.upper()}_API_SECRET")
        wallet_address = os.getenv(f"{exchange_name.upper()}_WALLET_ADDRESS")

        if not api_key or not api_secret:
            try:
                exchange_config_path = Path(f"config/exchanges/{exchange_name}_config.yaml")
                if exchange_config_path.exists():
                    with open(exchange_config_path, 'r', encoding='utf-8') as f:
                        exchange_config_data = yaml.safe_load(f)

                    auth_config = exchange_config_data.get(exchange_name, {}).get('authentication', {})

                    if exchange_name == "hyperliquid":
                        api_key = api_key or auth_config.get('private_key', "")
                        api_secret = api_secret or auth_config.get('private_key', "")
                        wallet_address = wallet_address or auth_config.get('wallet_address', "")
                    elif exchange_name == "lighter":
                        api_config = exchange_config_data.get('api_config', {})
                        auth_config = api_config.get('auth', {})
                        api_key = api_key or auth_config.get('api_key_private_key', "")
                        api_secret = api_secret or auth_config.get('api_key_private_key', "")
                    else:
                        api_key = api_key or auth_config.get('api_key', "")
                        api_secret = api_secret or auth_config.get('private_key', "") or auth_config.get('api_secret', "")
                        wallet_address = wallet_address or auth_config.get('wallet_address', "")

                    if api_key and api_secret:
                        self.logger.info(f"从配置文件读取API密钥: {exchange_config_path}")
            except Exception as e:
                self.logger.warning(f"无法读取交易所配置文件: {e}")

        # 创建交易所配置
        if exchange_name == "lighter":
            try:
                lighter_config_path = Path("config/exchanges/lighter_config.yaml")
                if lighter_config_path.exists():
                    with open(lighter_config_path, 'r', encoding='utf-8') as f:
                        lighter_config_data = yaml.safe_load(f)
                    api_config = lighter_config_data.get('api_config', {})

                    exchange_config = ExchangeConfig(
                        exchange_id="lighter",
                        name="Lighter",
                        exchange_type=market_type,
                        api_key="",
                        api_secret="",
                        testnet=api_config.get('testnet', False),
                        enable_websocket=True,
                        enable_auto_reconnect=True
                    )
                else:
                    exchange_config = ExchangeConfig(
                        exchange_id="lighter",
                        name="Lighter",
                        exchange_type=market_type,
                        api_key="",
                        api_secret="",
                        testnet=False,
                        enable_websocket=True,
                        enable_auto_reconnect=True
                    )
            except Exception as e:
                self.logger.warning(f"加载Lighter配置失败: {e}")
                exchange_config = ExchangeConfig(
                    exchange_id="lighter",
                    name="Lighter",
                    exchange_type=market_type,
                    api_key="",
                    api_secret="",
                    testnet=False,
                    enable_websocket=True,
                    enable_auto_reconnect=True
                )
        else:
            exchange_config = ExchangeConfig(
                exchange_id=exchange_name,
                name=exchange_name.capitalize(),
                exchange_type=market_type,
                api_key=api_key or "",
                api_secret=api_secret or "",
                wallet_address=wallet_address,
                testnet=False,
                enable_websocket=True,
                enable_auto_reconnect=True
            )

        # 使用工厂创建适配器
        factory = ExchangeFactory()
        adapter = factory.create_adapter(
            exchange_id=exchange_name,
            config=exchange_config
        )

        await adapter.connect()
        return adapter

    async def print_statistics(self):
        """定期输出统计信息"""
        while self._running:
            try:
                await asyncio.sleep(self.stats_interval)
                
                if not self.coordinator:
                    continue
                    
                stats = self.coordinator.get_statistics()
                grid_state = self.coordinator.grid_state
                
                self.logger.info("=" * 80)
                self.logger.info(f"网格交易统计 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                self.logger.info("=" * 80)
                self.logger.info(f"交易对: {self.coordinator.config.symbol}")
                self.logger.info(f"网格类型: {self.coordinator.config.grid_type.value}")
                self.logger.info(f"运行时长: {stats.uptime}")
                self.logger.info("")
                
                # 持仓信息
                self.logger.info(f"当前持仓: {grid_state.current_position}")
                self.logger.info(f"持仓成本: ${grid_state.average_price:.4f}")
                self.logger.info(f"当前价格: ${grid_state.current_price:.4f}")
                self.logger.info("")
                
                # 订单统计
                self.logger.info(f"活跃订单: {stats.active_orders}个")
                self.logger.info(f"累计成交: {stats.total_filled}单")
                self.logger.info(f"成交金额: ${stats.total_volume:.2f}")
                self.logger.info("")
                
                # 盈亏统计
                self.logger.info(f"已实现盈亏: ${stats.realized_pnl:.4f}")
                self.logger.info(f"未实现盈亏: ${stats.unrealized_pnl:.4f}")
                self.logger.info(f"总盈亏: ${stats.total_pnl:.4f}")
                self.logger.info(f"盈亏率: {stats.pnl_percentage:.2f}%")
                self.logger.info(f"年化收益率: {stats.apr:.2f}%")
                self.logger.info("=" * 80)
                
            except Exception as e:
                self.logger.error(f"输出统计信息失败: {e}")

    async def run(self):
        """启动后台运行"""
        # 配置日志
        setup_optimized_logging(use_colored=False)  # 后台模式不使用颜色
        
        if self.debug:
            logging.getLogger().setLevel(logging.DEBUG)
            for module in ['core.services.grid', 'core.adapters.exchanges', 'ExchangeAdapter']:
                logging.getLogger(module).setLevel(logging.DEBUG)

        self.logger.info("=" * 80)
        self.logger.info("网格交易系统 - 后台运行模式")
        self.logger.info("=" * 80)
        
        try:
            # 1. 加载配置
            self.logger.info("步骤 1/6: 加载配置文件...")
            config_data = await self.load_config()
            grid_config = self.create_grid_config(config_data)
            self.logger.info(f"✅ 配置加载成功")
            self.logger.info(f"   - 交易所: {grid_config.exchange}")
            self.logger.info(f"   - 交易对: {grid_config.symbol}")
            self.logger.info(f"   - 网格类型: {grid_config.grid_type.value}")

            # 现货做空校验
            symbol = grid_config.symbol
            exchange_name = grid_config.exchange.lower()
            is_spot = False

            if exchange_name == "hyperliquid":
                is_spot = ":SPOT" in symbol.upper()
            elif exchange_name == "backpack":
                is_spot = "_SPOT" in symbol.upper() or "SPOT" in symbol.upper()

            if is_spot and grid_config.grid_type.value in ["short", "martingale_short", "follow_short"]:
                self.logger.error("❌ 错误：现货市场不支持做空网格！")
                sys.exit(1)

            # 2. 创建交易所适配器
            self.logger.info("步骤 2/6: 连接交易所...")
            self.exchange_adapter = await self.create_exchange_adapter(config_data)
            self.logger.info(f"✅ 交易所连接成功: {grid_config.exchange}")

            # 3. 创建核心组件
            self.logger.info("步骤 3/6: 初始化核心组件...")
            strategy = GridStrategyImpl()
            engine = GridEngineImpl(self.exchange_adapter)
            grid_state = GridState()
            tracker = PositionTrackerImpl(grid_config, grid_state)

            # 创建预留管理器（仅现货）
            reserve_manager = None
            reserve_monitor = None

            if self.exchange_adapter.config.exchange_type == ExchangeType.SPOT:
                spot_reserve_config = getattr(grid_config, 'spot_reserve', None)
                if spot_reserve_config and spot_reserve_config.get('enabled', False):
                    reserve_manager = SpotReserveManager(
                        reserve_config=spot_reserve_config,
                        exchange_adapter=self.exchange_adapter,
                        symbol=grid_config.symbol,
                        quantity_precision=grid_config.quantity_precision
                    )
                    reserve_monitor = ReserveMonitor(
                        reserve_manager=reserve_manager,
                        exchange_adapter=self.exchange_adapter,
                        symbol=grid_config.symbol,
                        check_interval=60
                    )
                    self.reserve_monitor = reserve_monitor

            # 4. 创建协调器
            self.logger.info("步骤 4/6: 创建系统协调器...")
            self.coordinator = GridCoordinator(
                config=grid_config,
                strategy=strategy,
                engine=engine,
                tracker=tracker,
                grid_state=grid_state,
                reserve_manager=reserve_manager
            )
            self.logger.info("✅ 协调器创建成功")

            # 启动前检查（仅现货且启用预留管理）
            if reserve_manager:
                self.logger.info("启动前检查: 验证现货预留BTC...")
                if not await check_spot_reserve_on_startup(grid_config, self.exchange_adapter, reserve_manager):
                    self.logger.error("❌ 启动检查失败，系统退出")
                    await self.exchange_adapter.disconnect()
                    sys.exit(1)

            # 5. 启动网格系统
            self.logger.info("步骤 5/6: 启动网格系统...")
            await self.coordinator.start()
            self.logger.info("✅ 网格系统已启动")

            # 启动预留监控
            if reserve_monitor:
                await reserve_monitor.start()
                self.logger.info("✅ 预留监控器已启动")

            # 6. 启动统计输出任务
            self.logger.info("步骤 6/6: 启动监控任务...")
            self._running = True
            stats_task = asyncio.create_task(self.print_statistics())
            
            self.logger.info("=" * 80)
            self.logger.info("✅ 网格交易系统完全启动（后台模式）")
            self.logger.info("=" * 80)
            self.logger.info(f"日志文件: logs/ExchangeAdapter.log")
            self.logger.info(f"统计输出间隔: {self.stats_interval}秒")
            self.logger.info(f"使用 'kill -SIGTERM {os.getpid()}' 或 Ctrl+C 安全退出")
            self.logger.info("=" * 80)

            # 等待退出信号
            await self._shutdown_event.wait()
            
            # 取消统计任务
            stats_task.cancel()
            try:
                await stats_task
            except asyncio.CancelledError:
                pass

        except Exception as e:
            self.logger.error(f"❌ 系统错误: {e}", exc_info=True)
            raise

        finally:
            await self.cleanup()

    async def cleanup(self):
        """清理资源"""
        self.logger.info("正在清理资源...")
        self._running = False
        
        try:
            if self.coordinator:
                # 退出清理（如果启用）
                try:
                    await self.coordinator.cleanup_on_exit()
                except Exception as e:
                    self.logger.error(f"退出清理异常: {e}")
                
                await self.coordinator.stop()
                self.logger.info("✓ 网格系统已停止")

            if self.reserve_monitor:
                await self.reserve_monitor.stop()
                self.logger.info("✓ 预留监控器已停止")

            if self.exchange_adapter:
                await self.exchange_adapter.disconnect()
                self.logger.info("✓ 交易所已断开")

            self.logger.info("=" * 80)
            self.logger.info("✅ 系统已安全退出")
            self.logger.info("=" * 80)

        except Exception as e:
            self.logger.error(f"⚠️ 清理过程出错: {e}")

    def handle_signal(self, signum, frame):
        """处理退出信号"""
        self.logger.info(f"\n收到退出信号 (信号: {signum})，正在安全退出...")
        self._shutdown_event.set()


def parse_arguments():
    """解析命令行参数"""
    parser = argparse.ArgumentParser(
        description='网格交易系统 - 后台运行模式（适合云主机部署）',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 后台运行
  python3 run_grid_trading_daemon.py config/grid/lighter_btc_perp_long.yaml
  
  # 使用 nohup 后台运行（推荐）
  nohup python3 run_grid_trading_daemon.py config/grid/lighter_btc_perp_long.yaml > grid.log 2>&1 &
  
  # 使用 screen 会话运行
  screen -dmS grid python3 run_grid_trading_daemon.py config/grid/lighter_btc_perp_long.yaml
  
  # DEBUG 模式
  python3 run_grid_trading_daemon.py config/grid/lighter_btc_perp_long.yaml --debug

特点:
  ✅ 无需终端 UI，适合云主机
  ✅ 完整日志记录到文件
  ✅ 定期输出统计信息
  ✅ 支持优雅退出（SIGTERM/SIGINT）
  ✅ 所有网格功能完整支持

注意事项:
  1. 建议使用 nohup 或 screen 在后台运行
  2. 日志输出到 logs/ExchangeAdapter.log
  3. 统计信息每5分钟输出一次
  4. 使用 kill -SIGTERM <PID> 安全退出
        """
    )

    parser.add_argument(
        'config',
        type=str,
        help='网格配置文件路径'
    )

    parser.add_argument(
        '--debug',
        action='store_true',
        help='启用DEBUG模式'
    )

    parser.add_argument(
        '--stats-interval',
        type=int,
        default=300,
        help='统计输出间隔（秒），默认300秒'
    )

    parser.add_argument(
        '--version',
        action='version',
        version='网格交易系统 v2.0.0 (Daemon)'
    )

    return parser.parse_args()


async def main():
    """主函数"""
    args = parse_arguments()
    
    # 检查配置文件
    if not Path(args.config).exists():
        print(f"❌ 配置文件不存在: {args.config}")
        sys.exit(1)
    
    # 创建运行器
    runner = DaemonGridRunner(args.config, args.debug)
    runner.stats_interval = args.stats_interval
    
    # 注册信号处理
    signal.signal(signal.SIGTERM, runner.handle_signal)
    signal.signal(signal.SIGINT, runner.handle_signal)
    
    # 运行
    await runner.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 程序已退出")
    except Exception as e:
        print(f"\n❌ 启动失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
