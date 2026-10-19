#!/data/data/com.termux/files/usr/bin/sh
# T-Fin03 全流程盘控（crontab 日任务入口）：A股收盘后 15:30 交易日运行
# 台账 index/params.yaml → cron.schedule / cron.runner
cd "$HOME/finance-agent" || exit 1
echo "== run_daily_all $(date '+%F %T') =="

python3 scripts/fetch_data.py        || exit 1   # 1) 更新行情台账（明日复核缺口）
python3 scripts/daily_report.py      || exit 2   # 2) 核心引擎增量 + 每报
python3 scripts/ensemble_engine.py --daily --replay 20 || exit 3   # 3) 三腿赛马增量
python3 scripts/push_notification.py --push-auto  || echo "[warn] webhook 未配置（摘要见 cron.log）"

# 每周五收盘后追加：风控综合审计（时间锁+地狱模式）
if [ "$(date +%u)" = "5" ]; then
  python3 scripts/risk_control.py --audit || exit 4
  python3 scripts/report_generator.py --weekly || exit 6   # 周末深度复盘（含台账溯源）
fi
# 每周六（周末）：策略进化引擎（网格+OOS拦截，含 T-Fin04 赛马寻优）
if [ "$(date +%u)" = "6" ]; then
  python3 scripts/optimizer.py            || exit 5
  python3 scripts/optimizer.py --ensemble || exit 5
fi
echo "== run_daily_all done $(date '+%F %T') =="
