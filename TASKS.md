# TASKS.md — 任务台账（T-Fin23 起）

> 工作规则：
> 1. 执行任何任务前，必须先在本文件登记（`- [ ] 待执行` → 执行中改为 `- [~]`）。
> 2. 完成一项立刻标记一项（`- [x]`），并附完成日期与一句话交付说明。
> 3. 铁律继承 `AGENTS.md`：模拟盘、不接实盘、熔断不可被优化、LLM 无下单权、next-open 撮合统一记账。
> 4. 本文件与任务出处证据（报告/commit）挂钩，可追溯。

## 状态图例

`- [ ]` 待执行 · `- [~]` 执行中 · `- [x]` 已完成

## 未完成任务（2026-10-21 核定）

- [~] T-Fin31 云端灾备代跑设计（业主 2026-10-21 提问：手机冻结时可否云端代跑，
  信号恢复再同步）。现状核查：origin=github.com:zhangxuegel/finance-agent.git，
  本地 main **ahead 17 未推**；Termux 无对应 SSH 私钥（T-Fin12 后历次提交均在
  本地）；.gitignore 密钥纪律齐备；.github/workflows 尚无。
  设计稿已回业主（Actions 热备 runner + 单写水位防分叉），**待业主拍板三点**：
  ①repo 公私与是否允许 Actions 拉私有库（免费 runner 限公共库）；②密钥走
  Actions Secrets（SUPABASE/GLM 不落 code）；③单日幂等闸（engine --daily
  按 trade_date 水位跳过已跑日，防手机/云端双写分叉）。拍板后开工。


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
