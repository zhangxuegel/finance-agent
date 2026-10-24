#!/bin/bash
# run_daily_all.sh —— 收盘调度入口（T-Fin35 对账规则重构版）
#
# 业主 2026-10-24 业务规则（对账闸重构，流水线强制顺序）：
#   -1) git pull          接收云端状态回推（失败仅警告，由对账闸兜底判定）
#    0) 数据完整性检查     preflight.py --check（只读；不全 → 精确清单 → 停跑）
#    1) 强制对账           cloud_gate.py --gate（每次启动必跑，关上一轮之事；
#                          统一=允许记账；冲突=阻断；未完成=有界等待 15 分钟）
#    2) 记账/推进          仅在对账统一后进入，幂等护栏防双写：
#                          fetch → 台账未推进才跑 影子 match/ledger/review
#                          + daily_report + ensemble
#    3) 记账后再对帐       --gate（同 标准）→ 上云 → 状态回推 repo
#    4) 周五审计+周报 / 周六 optimizer（分析件，仍在对账统一之后）
#   「谁缺谁停」：本侧断网/凭证缺失 → 本侧停跑（exit 3），他侧不受影响。
# 台账：index/params.yaml → cron.schedule(本地 16:40) / 云端 workflow(15:50)
set -u
cd "$HOME/finance-agent" || exit 1
echo "== run_daily_all $(date '+%F %T') =="
LOG_TAG="[run daily]"

# -1) 接收云端状态回推
git pull --ff-only 2>/dev/null || echo "$LOG_TAG [warn] git pull 失败（对账闸将兜底判定）"

# 0) 数据完整性检查（只读；缺失清单精确打印 → 停跑）
python3 scripts/preflight.py
if [ $? -ne 0 ]; then
  echo "$LOG_TAG 信息不全 → 停跑（本轮零写入）"
  exit 1
fi

# 1) 强制对账闸（每次启动必跑；exit 0/2/3 语义见 cloud_gate.py --gate；
#    默认 60 分钟——T-Fin35 补充拍板：周一云端跑得慢也不误杀；超时会自动带
#    ERROR 告警推送然后本侧退出）
python3 scripts/cloud_gate.py --gate --wait-minutes "${GATE_WAIT:-60}"
gate_rc=$?
case "$gate_rc" in
  0) echo "$LOG_TAG 对账统一 → 允许记账" ;;
  2) echo "$LOG_TAG ⚠ 不可调和冲突 → 阻断记账（明细见上；人工 --resolve-eod 裁决）"
     exit 2 ;;
  *) echo "$LOG_TAG 对账未完成（本侧停跑，他侧不受影响；下轮重试）"
     exit 3 ;;
esac

# 记账幂等护栏说明：fetch 先行把行情推到最新，再用「台账/影子 eod 水位
# 是否到达行情最新交易日」判定记账是否已完成（云端回推/先前已跑一致时跳过，
# 防双写）。

# 2) 记账/推进（仅对账统一后可达）
python3 scripts/fetch_data.py || exit 1
read -r PORT_LAST SHADOW_MAX LATEST <<EOF
$(python3 - <<'PY'
import csv, json, os, sqlite3
with open("data/510300.csv", newline="", encoding="utf-8") as f:
    rows = list(csv.DictReader(f))
latest = rows[-1]["date"] if rows else ""
port = json.load(open("data/portfolio.json", encoding="utf-8"))
try:
    with sqlite3.connect("data/shadow_live.db") as c:
        shadow = c.execute("SELECT MAX(trade_date) FROM eod").fetchone()[0] or ""
except Exception:
    shadow = ""
print(port.get("last_date") or "", shadow, latest)
PY
)
EOF
echo "$LOG_TAG 台账水位：portfolio=$PORT_LAST / 影子eod=$SHADOW_MAX / 行情最新=$LATEST"
TF=${FIN_FAKE_TF:-$(date +%u)}   # 1=Mon ... 6=Sat, 7=Sun（FIN_FAKE_TF 仅沙盒演练用）

if [ "$TF" -le 5 ]; then
  if [ "$SHADOW_MAX" \< "$LATEST" ]; then
    echo "$LOG_TAG 影子账未推进 → 收盘补撮合/落库（--replay，真实 open 口径）"
    python3 scripts/shadow_broker.py --match  --replay || echo "$LOG_TAG [warn] 影子撮合异常"
    python3 scripts/shadow_broker.py --ledger --replay || echo "$LOG_TAG [warn] 影子落库异常"
    python3 scripts/shadow_broker.py --review --replay || echo "$LOG_TAG [warn] 推送可降级"
  fi
  if [ "$PORT_LAST" \< "$LATEST" ] || [ -z "$PORT_LAST" ]; then
    echo "$LOG_TAG 核心账本未推进 → daily_report + ensemble"
    python3 scripts/daily_report.py    || exit 2
    python3 scripts/ensemble_engine.py --daily --replay 20 || exit 3
    python3 scripts/push_notification.py --push-auto || echo "$LOG_TAG [warn] webhook 未配置"
  else
    echo "$LOG_TAG 记账已完成（云端回推/先前已跑）→ 幂等跳过，不双写"
  fi
  # 3) 记账后再对帐（同一标准）→ 上云
  python3 scripts/cloud_gate.py --gate --wait-minutes 3 || echo "$LOG_TAG [warn] 记账后对帐未统一（行内人工介入）"
  bash sync_all.sh || echo "$LOG_TAG [warn] 上云异常（本地已留痕）"
  # 4) 状态回推 repo（本地发生过写入才提交；沙盒无 .git 时静默跳过）
  if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    git add -A data index reports logs 2>/dev/null || true
    if ! git diff --cached --quiet; then
      git commit -q -m "local-runner: 本地记账/对帐留痕（T-Fin35 流水线）" \
        -m "preflight→gate→记账→gate→sync 链路痕迹；详见当日报告"
      git push 2>/dev/null || echo "$LOG_TAG [warn] 状态回推 push 失败（本地已留痕）"
    fi
  fi
  # 周五：风控综合审计 + 周末深度复盘（分析件）
  if [ "$TF" = "5" ]; then
    python3 scripts/risk_control.py --audit || exit 4
    python3 scripts/report_generator.py --weekly || exit 6
  fi
fi

# 周六：策略进化引擎（网格+OOS 拦截）——同样在对账统一之后
if [ "$TF" = "6" ]; then
  python3 scripts/optimizer.py            || exit 5
  python3 scripts/optimizer.py --ensemble || exit 5
fi

echo "== run_daily_all done $(date '+%F %T') =="
