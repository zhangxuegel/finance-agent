#!/bin/bash
# run_live_day.sh —— 影子实盘一日全流程runner（T-Fin05 验收入口）
#
# 正式 cron 分段（见 docs/shadow_live.md）：
#   08:45  live_monitor.py --plan-push        （预案推送，只读台账）
#   09:25  live_data_feed.py --heartbeat      （盘前心跳，失败→降级标记）
#   09:30  shadow_broker.py  --match          （真实开盘价撮合+跌停拦截）
#   09:30-15:00 每5分钟  live_monitor.py --monitor （盘中≤-2%紧急报警）
#   14:55  live_data_feed.py --heartbeat      （尾盘前心跳）
#   15:05  shadow_broker.py  --ledger         （真实EOD落库+Alpha+熔断继承）
#   15:10  shadow_broker.py  --review         （成交复盘推送+报告）
#
# --replay：单次跑通全流程（回放时钟，验收用；真实行情快照，无资金动用）
set -u
cd "$(dirname "$0")/.." || exit 1
REPLAY=""
[ "${1:-}" = "--replay" ] && REPLAY="--replay"
LOG="logs/live_run_$(date +%F).log"
mkdir -p logs
# ---- T-Fin06 全局物理开关守卫：PANIC_LOCK.flag 存在 → 全日停牌 ----
if [ -f PANIC_LOCK.flag ]; then
  echo "=== $(date '+%F %T') 人工干预锁定中，跳过今日交易（PANIC_LOCK.flag）===" \
      | tee -a "$LOG"
  python3 scripts/kill_switch.py --status | tee -a "$LOG"
  exit 0
fi
run() {
  echo "=== $(date '+%F %T') $* ===" | tee -a "$LOG"
  python3 scripts/"$1" ${2:-} $REPLAY 2>&1 | tee -a "$LOG"
  # 铁律：任何阶段退出码非0也不能放倒整日流程（脚本级双保险）
  return 0
}
{
  run live_monitor.py --plan-push          # 08:45
  run live_data_feed.py --heartbeat        # 09:25
  run shadow_broker.py --match             # 09:30
  run live_monitor.py --monitor            # 盘中采样点（回放=1拍）
  run live_data_feed.py --heartbeat        # 14:55
  run shadow_broker.py --ledger            # 15:05
  run shadow_broker.py --review            # 15:10
  run aggregate_performance.py             # 15:15 连续追踪聚合+胜率热力图+PANIC状态
} || exit 1
echo "=== 全流程完成 → $LOG ===" | tee -a "$LOG"
exit 0
