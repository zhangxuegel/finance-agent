#!/bin/bash
# sync_all.sh —— 一键增量同步到云端（T-Fin07 模块 4）
#
# 用途：本地每日收盘流程跑完（run_daily_all.sh → shadow --ledger 落库）后，
# 运行一次即可把本地新增的 eod/fills 增量推上云端数据库（Supabase）。
#
# 纪律：
#  · 幂等：水位 data/cloud_sync_state.json 断点续传，重复运行=空集
#  · 云端故障绝不放倒本地（退出码恒 0，仅 logs/live_error.log 留痕）
#  · 密钥从环境变量读（SUPABASE_URL / SUPABASE_KEY），或可选一个 config 文件
set -u
cd "$(dirname "$0")" || exit 1

# 密钥来源优先级：环境变量 → $HOME/.finance_agent_cloud → 项目根 .env（T-Fin09 自动生成）
[ -f "$HOME/.finance_agent_cloud" ] && . "$HOME/.finance_agent_cloud"
if [ -z "${SUPABASE_URL:-}" ] && [ -f ".env" ]; then
  . ./.env
fi

LOG="logs/cloud_sync_$(date +%F).log"
mkdir -p logs

if [ -z "${SUPABASE_URL:-}" ] || [ -z "${SUPABASE_KEY:-}" ]; then
  echo "=== $(date '+%F %T') 未配置 SUPABASE_URL/KEY —— 本地不受影响，云端未同步 ===" \
      | tee -a "$LOG"
  python3 scripts/sync_to_cloud.py --test | tee -a "$LOG"
  exit 0
fi

echo "=== $(date '+%F %T') 云端增量同步启动 ===" | tee -a "$LOG"
python3 scripts/sync_to_cloud.py --push 2>&1 | tee -a "$LOG"
rc=${PIPESTATUS[0]}
if [ "$rc" -ne 0 ]; then
  echo "=== 同步异常（本地不受影响）→ $LOG ===" | tee -a "$LOG"
  exit 0
fi
echo "=== 同步完成 → $LOG ===" | tee -a "$LOG"
exit 0
