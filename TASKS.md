# TASKS.md — 任务台账（T-Fin23 起）

> 工作规则：
> 1. 执行任何任务前，必须先在本文件登记（`- [ ] 待执行` → 执行中改为 `- [~]`）。
> 2. 完成一项立刻标记一项（`- [x]`），并附完成日期与一句话交付说明。
> 3. 铁律继承 `AGENTS.md`：模拟盘、不接实盘、熔断不可被优化、LLM 无下单权、next-open 撮合统一记账。
> 4. 本文件与任务出处证据（报告/commit）挂钩，可追溯。

## 状态图例

`- [ ]` 待执行 · `- [~]` 执行中 · `- [x]` 已完成

## 未完成任务（2026-10-21 核定）

- [x] T-Fin32 补跑 10-22/10-23 两日缺口 + cloud_gate 对账闸缺陷修复（✅2026-10-24 完成）
  - 现象：手机端 10-22/10-23 未跑日常链，fetch/daily_report/shadow ledger/推云全缺；
    云端 workflow 同样未代跑（双方 eod 水位同停 2026-10-21）。
  - 缺陷 1：`24c5e45(fix: skip reconcile on github actions)` 把 `recon()` 改成空壳桩，
    本地路径也 return None → `--mobile` 闸 `rep[k]` TypeError 全场崩（当日实弹回归）。
    ✅修复：recon() 非 GA 环境派发 `recon_original`；main --mobile 增 rep=None 守卫；
    selftest 11/11 全绿；真账 `--mobile` 对账=双向补齐 eod +2 行、零冲突。
  - 缺口 2 ✅：`daily_report --replay 3` 不能作账本校验器（见结构发现），账本从 git
    HEAD 归位后按增量口径 engine_run(report_from=10-22) 推进两日（零订单、净值
    ¥1,519,987.97 不变），10-22/10-23 两份日报按真账重生成。
  - 缺口 3 ✅：新增维护工具 `scripts/repair_shadow_backfill.py`（安全闸 A/B/C/D：
    缺日有成交即停手/有持仓即停手/灯色确定性重算/缺 K 线显式 eod_gap），
    影子链补 10-22(¥976,321.8)/10-23(¥976,302.4) 两行 EOD（真实日线收口，零成交）。
  - 缺陷 2 ✅：`report_generator --weekly` KeyError tilt_note（race_log rule 兜底行
    无该键）→ 自愈取 f"factor.source" 兜底，W43 周报生成成功。
  - 端到端：云端回读 sb_shadow_eod 尾 4 行 = 10-23/10-22/10-21/10-20，两侧一致；
    risk_control --audit（186 笔合规 PASS）+ validate_sim 全绿 + test_sim 全绿；
    fetch 覆盖 2023-10-25~2026-10-23（726 行）。
- [ ] T-Fin33（登记待拍板）：重放引擎与台账锚点口径的结构性分歧
  - 现象：`daily_report.py --replay N` 的全新账户起步用 `i0=warmup`（warmup 窗口后
    第一信号日建仓），而台账真账首日是 params.anchor.date=2023-11-17 的等权锚定
    建仓（engine 全链无实现、仅 audit 豁免引用）。当日实测：重放 186 笔中 185 笔
    与真账不同，10-23 净值差 ¥21,311（1,541,299 vs 1,519,988）。
  - 影响：①--replay 全量重放的数字与真账不可比，误用会导致"两个账本"；
    ②任何下游若把重放当校验基准都会误报（当日已排险：真账自 HEAD 归位）。
  - 候选处置（业主拍板其一）：a) engine 实现锚点建仓→重放=真账可当校验器；
    b) 只读侧维持现状，重放标记"演示口径"并在文档/register 登记不可互查。
    风控红线不动：熔断、LLM 无下单权均不受影响。

- [x] T-Fin31 云端灾备代跑（✅2026-10-21 完成：三点拍板落地 + 双源对帐修订令）
  - 拍板执行：repo 公共（业主面板切换）/ 手机优先（闸 0 exit 9 语义）/ HTTPS token。
  - 交付：`scripts/cloud_gate.py`（守门员模块）：
    ① `--cloud-runner`：云端单写判定——今日 eod 已落库 → exit 9 跳过（手机优先）；
    ② `--init-db`：runner 冷启动从云端分页整卷回灌 eod/fills（净值链不断；
       实测 400 定位 eod 无 id 列改各自 order 参数）；
    ③ `--recon [--fix] [--dry]`（业主"双跑对帐查残缺，补全同步两不误"修订令）：
       本地影子账↔云端全量对账（不按水位），残缺双向补齐（eod REPLACE 幂等、
       fills 按签名去重），同日双写冲突**停手不猜**交人工；eod 数值冲突按
       --prefer 覆盖（缺省手机优先 local）；selftest 11 项全绿（离线桩测）；
    ④ `--mobile`：手机第 0 步=对帐+补齐+冲突提示，闸结果不放倒日常链。
  - `run_daily_all.sh`：第 0 步接 `--mobile` 对帐闸；末步接 `sync_all.sh`
    （堵"跑完不推"缺口——T-Fin30 场景防复发）。
  - `.github/workflows/daily_close.yml`：cron UTC 07:50（北京 15:50，手机先手半档）；
    闸 0 单写判定 → 冷启动回灌 → shadow 补撮合/落库（--replay，真实 open）→
    fetch/daily_report/ensemble → 闸 2 同步前对帐 → --push → 状态回推 repo
    （checkout 自带 GITHUB_TOKEN 认证）；并发排队不互杀；workflow_dispatch 手动补跑。
  - 验收：test_sim 全绿 ✔；cloud_gate selftest 11/11 ✔；真账对帐全空残缺
    （eod/fills 双侧一致、零冲突）。
  - ~~**待业主**（上云三键）~~ ✅✅✅ **2026-10-21 全部闭环**：
    ①repo 已 Public（API 核验 private:false）；②Actions Secrets 已配
    SUPABASE_URL/KEY（API 密钥列表核验；GLM_API_KEY 可选暂缺，云端日报走 rule
    兜底）；③临时 PAT（repo+workflow scope）由 Agent 推送成功
    86ff477..d57342c（19 提交，workflow 文件落库核验 200），
    云端 workflow 自此自持运行。备忘：首枚 PAT 因缺 workflow scope 被
    GitHub 拒（refusing to allow … without `workflow` scope），换纹后通过。


- [x] T-Fin23 接入 GLM LLM 分析师（✅2026-10-21 完成）
  - 交付：①key 注入 `~/.bashrc` export GLM_API_KEY（用户自置，不进 git）；
    ②单点安全验收：`daily_report.llm_review` → `mode: llm`、真实回复语义正确
    （CRO 四问通路 endpoint/key/model 三打通，model=glm-4-flash）；因子链
    `factor_mining.llm_score` 真实调用穿透至 `_guarded` 硬逻辑闸——极端牛市
    LLM 未给 90+ → `overridden(硬规则接管)`，**防幻觉硬闸实弹生效**（设计
    行为，pipeline 不崩）；③crontab 头部 env 注入（用户自置，单用户本地作用域）。
  - 影响面：`daily_report` 的 CRO 点评从 rule 兜底升级为真实 GLM（下一交易日
    15:30 cron 自动生效）；因子分链路可用（实盘日 LLM+新闻）。红线不动：
    LLM 无下单权，输出只进 tilt 位/reports 文字区。

- [x] T-Fin24 优化器网格全 OOS 崩塌问题（✅2026-10-21 完成，走验收分支 b"有台账依据的维持拦截"）
  - 交付：a) `ENSEMBLE_GRID` 16→48 组，新增 `race.winner_share·loser_share`
    (0.7/0.3、0.8/0.2、0.6/0.4)；报告网格维度行同步。
  - 结论：48 组全历史 MDD 2.3–3.8%（0 组触 MDD≤12% 红线），但 OOS 夏普全部
    ≤0 → 逐组拦截、confidence low。数值证据：OOS 63 交易日累计收益 -0.0
    （近零）而夏普 -16.5 → 分母（波动）趋零导致的伪读数，即台账附录
    T-Fin04 追加第 6 条已登记现象；组间 Δ 夏普 ≤0.26 属噪声，不可排序采信。
  - 处置：维持 T-Fin03 默认参数，不采纳任何网格候选；winner/loser share
    对 OOS 的影响（均值 -16.613 vs -16.633）在噪声带内，无证据支持调参。

- [~] T-Fin25 胜率改善观察（依赖 T-Fin24 ✅，观察口径不变）
  - 现状：累计胜率 40.86%（93 笔）< 45% 参考线，健康度连续扣此一分。
  - 待办：只随 T-Fin24 的合法寻优结果改善；**不直接改策略逻辑**、不动熔断。
  - 验收：胜率 ≥ 45% 或连续 4 周 > 42% 且夏普 ≥ 1.0。

- [x] T-Fin26 冻结收尾归档（✅2026-10-21 完成：reports/freeze_2026-10-21.md +
  reports/freeze_2026-10-21.sha256 八文件指纹锁定（活台账 TASKS.md 由 git 定格）；原急跌门评估稿顺延为 T-Fin28 冻结）

- [x] T-Fin28 多策略赛马与因子接入（✅业主 2026-10-21 书面改版：原“急跌门放宽评估”
  顺延为 T-Fin29，冻结中、需 CRO 书面确认才开工。**2026-10-21 完成**：四腿 D 网格+E 红利、
  factor RSI14/ATR 接入、validate 37✅ 全绿、crash 双 PASS、race_compare 赛马报告
  reports/race_T-Fin28_2026-10-21.md；文档 docs/ensemble_and_factor.md §T8）
  - 交付范围：①新增 D 腿（震荡网格：MA120 基线 ±3% 单格级联；RSI 腿 B 保留，
    4 腿 A/B/D/E 周赛马按 20 日夏普分权）；②E 腿（防守红利：510880 红利ETF，
    台账登记真实分红历史 + defensive 标记，红灯不清仓豁免）；③factor_mining
    接入 RSI14 + ATR% 辅助过滤（rule_score 修订 + 极端情景矩阵回归）；④
    红灯状态机：A/B/D 强制降至 0，E 保留（validate V3 适配）；⑤全量历史回测
    （含黑天鹅重放）+ 策略赛马对比报告。
- [x] T-Fin30 云端同步失效应急（✅2026-10-21 完成：诊断+放行全闭环）
  - 诊断（前报）：根因=Supabase 服务端撤销原 secret key（10-20 01:06 成功→
    13:08 起 401 Unregistered API key）；cron 实证 crond 会执行 spool 任务，
    crontab 今日 16:20 装载、15:30 时段未到。crond 已守候、原 crontab 已还原。
  - 放行：业主提供新 sb_secret key → 双写 ~/.finance_agent_cloud +
    .streamlit/secrets.toml（600）→ `sync_all.sh` upsert **eod +1 行成功**，
    水位 max_eod_date 推进到 **2026-10-21**；云端回读
    `sb_shadow_eod` 尾两行 = 10-21 ¥976,312.1 / 10-20 ¥976,273.3 与本地一致。
  - 后续约定：密钥今后只改两处凭证文件；云端 upsert 幂等，重推无副作用。
  - ① 手动全量收盘流程已跑：run_daily_all.sh 全链绿（fetch/daily_report/ensemble
    --daily/push），eod REPLACE 幂等核验 10-21 行=1；NAV ¥958,266（红灯日 0 交易）。
  - ② **云端根因＝Supabase 密钥失效**（非 cron、非本地缺口）：
    `logs/cloud_sync_2026-10-21.log HTTP 401 "Unregistered API key"`；
    时间线：10-20 01:06 同步成功（水位=eod 10-20）→ 10-20 11:38~14:55 本地
    `.finance_agent_cloud` 与 `.streamlit/secrets.toml` 均带同一 `sb_secret_vxoL…`
    → 13:08 起全部 401 ⇒ **Supabase 服务端把该 secret key 轮换/撤销**（或项目
    暂停恢复时密钥重置）。双头传输（apikey+Bearer 同发）已排除传输方式问题。
    本地无 ~/.supabase_token/SUPABASE_ACCESS_TOKEN，无法经 Management API 自愈。
    **待业主**：Supabase Dashboard → Project Settings → API Keys → 生成新
    secret key → 写入 `~/.finance_agent_cloud` 的 SUPABASE_KEY 行（另同步更新
    .streamlit/secrets.toml）；然后 `bash sync_all.sh`（水位断点续传，幂等）。
  - ③ cron 判定：crond 存活且**经过控探针实证会执行 spool 任务**（首探误报系
    cron 会把命令在首个 `%` 截断的既知特性）；但整个 `var/spool/cron` 目录
    mtime=2026-10-21 16:03 —— crontab 是**今天 16:20 才装载**，15:30 时段本就
    未到达，近几天未同步与此无关。已还原原 crontab（探针已撤）、crond -b 拉起。
    **手机侧防冻结指引**（Termux 请勿被电池优化杀后台）：
    a) 系统设置 → 应用管理 → Termux → 电池 → 改"无限制"/关闭电池优化；
    b) 多任务卡片上锁 Termux；
    c) 系统设置 → 电池 → 关闭"自适应电池"对 Termux 的深睡（厂商 ROM：自启动
    白名单/关联启动勾选 Termux）；
    d) Termux 新会话执行 `termux-wake-lock`（可加进 run_daily_all.sh 用户侧入口，
    或放 crontab 每天 15:20 `termux-wake-lock`）；
    e) 勿用一键清理/后台管理器划掉 Termux；Android 版本若支持，开启
    " Phantom process killer 关闭 "（`adb shell settings put global
    settings_enable_monitor_phantom_procs false` 或开发者选项关闭"后台进程限制"）。
  - 验收：本地 eod=10-21 齐；**云端放行**：业主交新 sb_secret key → 双写
    ~/.finance_agent_cloud + .streamlit/secrets.toml（600，不回显）→
    `sync_all.sh` upsert **eod +1 行成功**，水位 max_eod_date 推进到
    **2026-10-21**；云端回读 sb_shadow_eod 尾两行 = 10-21 ¥976,312.1 /
    10-20 ¥976,273.3 与本地一致（看板即恢复当日数据）；cron 明日 15:30
    首个交易日时段自动首跑并落 data/cron.log 留痕。
  - 后续约定：密钥今后只改两处凭证文件即可（勿重复轮换）；云端 upsert 幂等，
    重推无副作用。
- [ ] T-Fin29 急跌门放宽评估（原 T-Fin28 定义顺延，冻结中，需 CRO 书面确认才开工）

- [x] T-Fin27 回归红线失效：撮合时间锁账本审计失败（✅2026-10-21 完成）
  - 根因：账本(created 2026-10-19)按腾讯 raw 不复权口径记账；10-21 大重抓把
    data/510300.csv 整卷换成东财前复权（fqt=1）→ 审计基准撕裂产生 46 条伪违规
    （44 价差 + 5 信号重算跳点失真 + 1 forced 卖价，部分笔同时双违规）。git
    e6f93c9 的 CSV（2023-11-17 open=3.63, amount=0=腾讯格式）实证账本口径。
  - 修复：a) `index/params.yaml` 登记 510300 真实分红台账（东财 fhsp 公告 2024-01-18
    /2025-06-18/2026-01-19 除息，每股 0.069/0.088/0.123——与账本-CSV 三档台阶
    精确互证）+ 开局锚点日 2023-11-17 登记；b) `audit_fill_time_lock` 升级为
    口径自洽版：raw = qfq + Σ{除息日>d}每股分红（该反推对补登分红具有不变性），
    信号重算同步在 raw 序列上；锚点日 BUY 豁免前日信号校验。
  - 验收：test_sim 全绿 ✔（186/186 合规，锚点豁免 3 笔）；validate_sim V1–V12 全绿 ✔；
    risk_control --audit PASS。资产净曲线/撮合逻辑零改动（审计口径修复）。
  - 维护规则：今后标的除息须及时在 params.yaml dividends 登新事件，否则审计报价差提醒补台账。

## 已完成（近期,示例存档）

- [x] T-Fin22 云端看板顶部遮挡修复（968b8fd，UI-only，selftest 全绿）
- [x] T-Fin14 云端读取修复——secrets 键名兼容矩阵（f2aaf18）
- [x] T-Fin12 云端看板竖屏重构（6a3d8a9）
- [x] T-Fin10 GitHub 推送准备（c5c5e70）
- [x] 2026-10-21 每日周期全量落库（ecc7a87）
