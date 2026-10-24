#!/bin/bash
# run_daily_all.sh —— 收盘调度入口（T-Fin34 起：云端为主线、本地对帐落库）
#
# 业主 2026-10-24 拍板改版（T-Fin31 的"手机优先"反转）：
#   -1) git pull   接收云端 cloud-runner 的状态回推（repo=账本真源的传播通道）
#    0) cloud_gate --mobile  双源对帐（残缺回灌 / 冲突停手；幂等）
#    0.5) 判云：若本地影子 eod 已含【今日 SH 交易日】（云端已单写完成）→ 本轮
#         只做对帐收尾，不重复落库（单日单写者纪律）。等待规则：若云端
#         workflow 仍 run-in-progress（GitHub Actions 公共 API 免凭证可查），
#         至多等 WAIT_TIMEOUT_MIN 再判一次；云端失败/无记载才进入本地灾备。
#    1+）本地灾备链（仅云端缺日时）：影子补撮合/落库(--replay) → fetch →
#        daily_report → ensemble → 对帐闸 → 上云 → 状态回推 repo。
#   周五：风控综合审计 + 周末深度复盘（分析件，不触碰账本）。
#   周六：策略进化引擎（网格+OOS 拦截）。
#
# 台账：index/params.yaml → cron.schedule(本地错峰 16:40) / workflow(云端 15:50)
set -u
cd "$HOME/finance-agent" || exit 1
echo "== run_daily_all $(date '+%F %T') =="
LOG_TAG="[run daily]"

# -1) 接收云端状态回推（网络失败不阻断本地）
git pull --ff-only 2>/dev/null || echo "$LOG_TAG git pull 失败（离线/无缘——继续本地）"

# 0) 双源对帐闸（残缺回灌；冲突停手提示人工）
python3 scripts/cloud_gate.py --mobile || echo "$LOG_TAG [mobile] 对帐闸异常（凭证缺失=放行本地主链）"

# 判云辅助：GitHub Actions 最近一次 daily_close 运行状态（公共 API，无凭证）
gh_run_state() {
  curl -fsS --max-time 15 \
    "https://api.github.com/repos/zhangxuegel/finance-agent/actions/workflows/daily_close.yml/runs?per_page=1" 2>/dev/null \
  | python3 -c "import json,sys
try:
    r=json.load(sys.stdin)['workflow_runs'][0]
    print(r['created_at'][:10], r['status'], r['conclusion'] or '-')
except Exception:
    print('unknown')" \
  || echo "unknown"
}

# 本地影子 eod 是否含今日 SH 交易日（=今日已完成单写）
have_today_eod() {
  python3 scripts/cloud_gate.py --cloud-runner >/dev/null 2>&1
  [ $? -eq 9 ]   # exit 9 = 今日 eod 已落库（单写闸语义复用）
}

# 0.5) 判云
TF=$(date +%u)   # 1=Mon ... 5=Fri, 6=Sat, 7=Sun
if [ "$TF" -le 5 ]; then
  WAIT_MIN=15
  waited=0
  while [ $waited -lt $WAIT_MIN ]; do
    if have_today_eod; then
      echo "$LOG_TAG 判云：今日 eod 已由云端单写落库 → 本轮对帐收尾，不重复落库"
      if [ "$TF" = "5" ]; then
        python3 scripts/risk_control.py --audit || exit 4
        python3 scripts/report_generator.py --weekly || exit 6
      fi
      exit 0
    fi
    state=$(gh_run_state)
    today=$(date +%F)
    case "$state" in
      "$today in-progress"*|"$today pending"*|"$today queued"*|"$today waiting"*)
        echo "$LOG_TAG 判云：云端 workflow 进行中（$state）→ 等待 …"
        sleep 60; waited=$((waited + 1)) ;;
      *)
        break ;;
    esac
  done

  # 云端缺日 → 本地灾备全链（= workflow 代跑本地版，逐一步等价）
  echo "$LOG_TAG 判云：今日云端未落库 → 本地灾备链启动"
  set +e
  python3 scripts/shadow_broker.py --match   --replay   # 真实 open 撮合（收盘后口径）
  python3 scripts/shadow_broker.py --ledger  --replay   # eod REPLACE 落库
  python3 scripts/shadow_broker.py --review  --replay   # 推送可降级
  set -e
  python3 scripts/fetch_data.py      || exit 1
  python3 scripts/daily_report.py    || exit 2
  python3 scripts/ensemble_engine.py --daily --replay 20 || exit 3
  python3 scripts/push_notification.py --push-auto || echo "[warn] webhook 未配置"
  # 同步前对帐（同 workflow 闸 2 语义）
  python3 scripts/cloud_gate.py --mobile --dry || true
  python3 scripts/cloud_gate.py --mobile || true
  set +e
  bash sync_all.sh
  git add -A data index reports logs 2>/dev/null || true
  if ! git diff --cached --quiet; then
    git commit -q -m "mobile-runner: 本地灾备落库（云端缺日接管）" \
      -m "T-Fin34：云端为主线，本地仅在缺日时单写灾备；详见当日报告"
    git push 2>/dev/null || echo "$LOG_TAG [warn] 状态回推 push 失败（本地已落库）"
  fi
  set -e
fi

# 周六（周末）：策略进化引擎（网格+OOS 拦截，含 T-Fin04 赛马寻优）
if [ "$TF" = "6" ]; then
  python3 scripts/optimizer.py            || exit 5
  python3 scripts/optimizer.py --ensemble || exit 5
fi

echo "== run_daily_all done $(date '+%F %T') =="
