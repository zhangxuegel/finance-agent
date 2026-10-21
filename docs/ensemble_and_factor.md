# T-Fin03 架构说明：多策略赛马调度与 LLM 因子挖掘

> 前置：T-Fin01/02 底层（确定性信号、next-open 撮合、双熔断、时间锁审计、
> 地狱模式、环境感知、CRO 硬注入）全部继承，本项目任何模块不得绕过。
> 红线速查：LLM 不下单 → 只调权；熔断规则绝对；黄/红灯 BUY → FatalControlError。

date: 2026-10-19 · 对应任务书：T-Fin03

## 1. 三腿结构（ensemble_engine.py）

| 腿 | 类型 | 进/出规则（确定性公式） | 台账 |
|---|---|---|---|
| A | 趋势跟随（MA 双均线） | MA20 上穿 MA60 → 1；下穿 → 0 | `ensemble.A` |
| B | 均值回归（RSI14） | RSI14 < 30 → 1；> 70 → 0；区间内维持（Wilder RSI） | `ensemble.B` |
| C | 避险防守（货币 ETF 511990） | 🟡黄灯/🔴红灯 或 A/B 同时空仓 → 全部现金买入货币 ETF；禁止现金空转（<一手尾差仅在台账 `park_residual` 登记） | `ensemble.C` |

- A/B 各自维护独立"腿账本"（cash/positions/trades/curve），同日对同一标的
  并发开仓时**各自用自己的现金**，成交与费用互不串账（validate_sim V2）。
- C 腿按"归属腿"记账：每次停泊（park）都记录 leg ∈ {A, B, MASTER}，
  赎回时资金回到原腿，账户恒等式（合并净值 = 母现金 + Σ腿权益）全程可验。
- 腿净值曲线每日记录，是赛马与合并账户熔断的唯一依据。

## 2. 赛马调度（每周最后一个交易日收盘后）

```text
sharpe_A = 腿A过去20日净值日收益年化夏普
sharpe_B = 腿B同口径
--------------------------------
胜者权重     = winner_share(0.7) / loser_share(0.3)
平滑(clamp)  = clip(权重, prev±max_weekly_shift(0.2), [0.3, 0.7])
              → 一周最多漂移 ±20pp，禁止 0→100% 赌注式跳变
LLM倾斜(可选)= score>80 → A +5pp；score<20 → C预配 10pp（Python clamp）
执行时点     = 下周首个交易日开盘，只做现金级划转（不动持仓，避免churn）
数据未满窗口 = 固定 5:5（warmup_fixed_share）
```

-防过拟合三道闸：绝对下限 0.3（拒绝 0:10 押注）、±20pp 周漂移上限、
  赛马只用"过去 20 日"滚动窗口（无未来数据）。
- 调度函数 `race_weights()` 为纯函数，validate_sim 用 40 组随机夏普回放
  验证其不越界。

## 3. LLM 因子挖掘（factor_mining.py）

### 3.1 因子（全部 Python 确定性计算）

| 因子 | 公式 | 替代口径说明 |
|---|---|---|
| 动量 momentum20 | close[-1]/close[-21] − 1 | —— |
| 波动率 atr_pct | Wilder ATR(14) / close | —— |
| 估值 valuation_q | 基准收盘在近 250 日的分位（0~1） | **ETF 无 PE 台账，禁用幻觉 PE**；以价格分位为确定性代理并在台账登记 |

### 3.2 LLM 打分链路与三层防线

```text
因子快照 + 新浪财经快讯标题(≤12条, 抓取失败→空列表不崩溃)
   ↓ GLM (prompt=任务书原文, 只输出 JSON {"score": 0~100})
① 解析防线: 非 JSON/缺 score/NaN → 硬编码降级 50 分
② 范围防线: 数值超 [0,100] → Python 侧行 clip（≈np.clip，Termux 无 numpy）
③ 隔离防线: 得分只进入 race_weights 的 tilt 位 → 只调权，绝不生成订单
无 API KEY → 规则兜底得分（momentum/灯色/估值线性映射，source 标注 rule）
```

- 得分来源全程留痕：`data/factor_log.jsonl`（每次调权可追溯）。
- 回放/历史模式下新闻**不可回溯**（时间锁纪律），规则得分只依赖因子——
  同一日期永远重现同样的分数，回测才可信；实盘当日才允许真实新闻+GLM。

## 4. 状态机与风控红线（优先级最高）

1. 灯色由 regime_filter 确定性给出；**黄/红灯日任何腿 BUY →
   FatalControlError，当日运行立即终止**（validate_sim V3a/V3b + 全流水
   扫描 V3e：黄/红日零 BUY）。
2. 合并账户同样受双熔断管辖（单日 -3% / 峰值回撤 -10% + 锁 3 日）；
   触发即双腿强平 + 货币腿赎回 + 残现金停泊。
3. 账户恒等式每日报表内 asserted：合并净值 = 母现金 + Σ腿权益，
   腿权益 = 腿现金 + 持仓市值 + 该腿名下货币份额市值。

## 5. 每日自动化与推送（push_notification.py + run_daily_all.sh）

crontab（台账 `cron.schedule` = `30 15 * * 1-5`，A股收盘后）：

```cron
30 15 * * 1-5  cd ~/finance-agent && sh scripts/run_daily_all.sh >> data/cron.log 2>&1
```

run_daily_all 流水：fetch_data → daily_report → ensemble --daily →
push_notification --push-auto；周五追加 risk_control --audit；周六 optimizer。

推送摘要内容（全部来自系统台账）：红绿灯/当日盈亏/净值/夏普·索提诺·卡玛/
资金使用率；并引用最新 risk_audit 报告结论条数。
防误发双保险：默认只允许 loopback mock；真实群 URL 需要
`--allow-real` + 环境变量 `FIN_PUSH_ALLOW=1` 同时成立（V5b 已验证拦截）。
Webhook 预留飞书/Telegram：`FIN_PUSH_URL` 环境变量注入 URL 即切换。

## 6. 概念图谱（T-Fin01 → 02 → 03 挂接，0 断链）

| T-Fin03 概念 | 挂接点 | 台账 |
|---|---|---|
| 三腿赛马 | ensemble_engine.EnsembleEngine + race_weights | ensemble.* |
| C 腿（货币防线） | park_money/redeem_money → 511990 | ensemble.C + watchlist(511990, exclude_core) |
| LLM 因子倾斜 | factor_mining.llm_score → tilt 位 | ensemble.tilt.* + llm.* |
| 黄/红灯状态机 | assert_gate → FatalControlError | regime_filter.traffic_light |
| 平滑防过拟合 | race_weights clamp | ensemble.race.max_weekly_shift/min_weight |
| 熔断继承 | risk_control.check_breakers（合并账户） | risk.* （零改动，红线继承） |
| 时间锁继承 | 信号只用 ≤t-1 数据、成交=t 日开盘 | fees.* （零改动） |
| 环境感知继承 | regime_filter（T-Fin02）→ 集成进 money_mode | benchmark.* |
| CRO 继承 | 摘要/日报中 Sortino·Calmar·审计结论 | push 章节 |

## 7. 已知口径与限制（诚实登记）

1. C 腿现金尾差：货币 ETF 一手 100 股（≈¥1 万），不足一手零头次日在
   `park_residual` 登记机关——"禁止现金空转"的例外，台账见指标。
2. 赛马"只动现金不动持仓"：再平衡不做强制调仓（防 churn 与冲击成本），
   权重通过下次自然换仓逐步收敛到目标。
3. 历史回放中新闻不可回溯 → 因子倾斜走确定性规则分；实盘当日才启用
   真实 GLM+新闻（--llm 且 GLM_API_KEY 存在）。
4. 本次三腿回测净值 +2.15%（夏普 -0.037）：A/B 信号在 2024–2026 下行
   环境弱，主要收益来自 C 腿利息；SR 大幅弱于 T-Fin01 单引擎（1.084），
   属于"机制正确、参数未优化"的诚实结果——优化路径：optimizer 的
   网格+OOS 拦截下周即可覆盖 ensemble.race 参数（win/loser share）。
5. （T-Fin04 追加）急跌门 0.99 高点回归 + 出闸三条件常设生效后在无急跌
   的横盘环境中过于保守：2024–2026 重放净值 -2.37%、MDD 2.43%——
   "先保住命"优先于"每 1bp 都在场"；放宽需 CRO 书面确认并改台账。
6. （T-Fin04 追加）OOS 夏普在防守期占满的样本（净值近水平、波动趋零）
   会给出 -16 档的非线性读数——optimizer 已按红线拦截（confidence=low），
   该读数只用于拦截判定，不作业绩宣传口径。

---

# T-Fin04 附录：寻优拦截 × 因子硬审计 × 晨周报 × 黑天鹅拷问

> 2026-10-19 交付。四模块全部建立在前三期台账与红线之上（熔断/时间锁/LLM 无下单权不变）。

## T1. 引擎升级：参数覆盖 / 窗口重放 / 独立数据目录

`EnsembleEngine(param_overrides, window, data_dir, log_writes)` 四口：
- `param_overrides`：仅允许 `ensemble.<亚层>.<叶子>` 三段键，optimizer 网格用；
- `window=(start,end)`：窗口重放（信号仍在全历史上预计算并按日期索引，灯色用
  基准 CSV **全历史前缀 ≤ t-1**——窗口起点之前的历史照样生效，无未来函数）；
- `data_dir`：独立数据目录（`data/crash/` 长历史黑天鹅与正式台账隔离）；
- `log_writes=False`：不追加 `data/ensemble_race_log.jsonl`（寻优/崩溃测试防污染台账）。
回归测试：默认全重放与 T-Fin03 逐分毫一致；窗口重放两次运行曲线完全相同（V8）。

## T2. 赛马网格寻优（Module 1）

- 网格 16 组：`race.max_weekly_shift{0.10,0.20} × race.min_weight{0.30,0.40} ×
  A 双均线{(20,60),(10,60)} × B RSI{(30,70),(25,75)}`，`optimizer.py --ensemble`；
- 训练/样本外切分：OOS = 最后 63 交易日（≈3 个月，未参与训练）；
- 绝对红线：全历史 MDD > 12% → 直接淘汰，绝不产出建议；
- OOS 拦截：训练夏普高但 OOS 夏普崩塌 >50% 或 OOS ≤ 0 → 拦截、confidence=low，
  建议只写台账 `index/ensemble_suggested.json`，**不自动改 params.yaml**；
- 输出 `reports/optimizer_ensemble_<date>.md`（基线 vs 最优对照 + Top-6 + 结论）。
诚实结果（2026-10-19，急跌门全开后的引擎）：16 组候选全历史 MDD 1.88%–3.46%
（远低于 12% 红线，无一淘汰）；OOS 段（最后 63 交易日）处于"几乎全程防守"
状态，净值近平，夏普呈现 -16 档的"零波动近似"读数 → **拦截机制全量触发**，
最优组 confidence=low，参数仅入台账不自动生效（需人工/CRO 确认）。
这说明 OOS 夏普在"防守期占满"的样本上会失真——已登记为已知口径（见 7.5）。

## T3. LLM 因子分硬逻辑审计（Module 2）

`factor_mining.audit_factor_logic`（在任何 llm_score 出口自动佩戴）：
- 极端牛市情景（连续 10 日涨停合成数据）→ 合规分数必须 ≥90；
- 极端熊市情景（连续 10 日 −5% + 利空新闻）→ 必须 ≤10；
- LLM 违背常识（如暴跌给 90 分）→ 抛 `FactorBiasError`、写
  `data/factor_bias_log.jsonl`（`LLM_FACTOR_BIAS`）、**score 硬接管为规则分**；
- 管线不因审计中断：错误在出口层被接收，但接管后的分数一定是确定性规则分；
- CLI：`python3 scripts/factor_mining.py --audit`（规则分 sanity R1–R2 +
  LLM 注入矩阵 L1–L6），合入 `--test` 与 validate_sim V7。

## T4. 晨报与周末深度复盘（Module 3）

`scripts/report_generator.py --daily / --weekly`：
- 晨报（`reports/morning_<date>.md`，cron 08:00 交易日）：灯色、合并净值与昨日盈亏、
  可用资金、持仓分布（腿×标的×占比）、当前赛马权重——全部系统台账数字；
- 周末深度复盘（`reports/weekly_<ISO周>.md`，cron 周六）：本周合并+三腿对比、
  赛马权重演变表（每周末 A/B 分配+C 预配+依据）、CRO 周度风控总结；
- 两份报告尾部都带 **台账溯源** 节：本次数字引用了 params.yaml 的哪些条款
  （race/tilt/C/risk/fees 全部列出键名与当前值）。

## T5. 黑天鹅崩溃测试（Module 4）

`scripts/crash_test.py`：两段**真实历史** × 完整系统（三腿+赛马+熔断继承+时间锁）：
- 2015 A股千股跌停 → `data/crash/` 长历史（东财单段直取，限流时腾讯 9 个月/段
  分段拼装+去重，口径与正式台账同源）；
- 结论（2026-10-19）：**双情景 PASS**
  | 情景 | 熔断起点 | 避险时滞 | 窗口 MDD | 判定 |
  |---|---|---|---|---|
  | 2015 千股跌停 | 2015-06-18 | +2 交易日（≤2 达标） | 4.06% ≤5% | ✅ |
  | 2020 美股熔断 | 2020-02-03 | +0 交易日 | 0.90% ≤5% | ✅（窗口末 +2.08%） |
- 失败路径即诊断路径：逃生超期/超撤时自动生成链路推演
  （逐日：基准、灯、A/B 敞口、C货、净值、日变动）；
- 场景期间发现并修复 1 个真实缺陷：黄灯日名下尚有货币份额时双腿持仓
  "既不清也不兑现"的清仓条件窄化 bug。

## T6. 急跌熔断门（崩溃测试催生的台账化防线）

崩溃测试代际教训 → `ensemble.crash_gate.*`（确定性数学，LLM 无关）：
- 触发：基准最近完成一笔收盘单日跌幅 ≤ `-2%` → 次日开盘起全面防守（pаrk 511990）；
- 出闸三条件（全部只使用 ≤ t-1 的收盘，无未来函数）：
  ① 灯回绿（close×MA60×MA120）；② 近 5 交易日无 ≤−2% 单日急跌（反绿闪）;
  ③ 高点回归：基准收盘 ≥ 60 日高点 × 0.99（防 V 型反弹"悬空绿灯"）；
- 逐资产版（复进观察期内）：未回归自身 60 日高点×0.99 的标的当日信号置零
  （2020 场景的关键：基准 510300 已回稳时 QDII 513500 仍独立崩盘）；
- 观察期（10 交易日）买入预算衰减 50%（防复进首日咬满仓）；
- 全历史效用（2023-10→2026-10）：MDD 9.64% → **2.43%**；净值 +2.15% → −2.37%
  （防御优先的诚实代价——环境弱时绝大多数日子置防守态，收益主要交给货币腿）。

## T7. 验收数字（2026-10-19 全绿）

- `test_sim.py`：T-Fin01/02 回归全部通过（无未来函数 ×40 探针、撮合时间锁 185 笔 0 违规）；
- `factor_mining.py --test / --audit`：降级矩阵 + 常识审计矩阵全部通过；
- `push_notification.py --test`：3/3；
- `validate_sim.py`：V1–V8 全部通过（新增 V7 因子审计、V8 窗口确定性）；
- `optimizer.py --ensemble`：报告+台账落盘，拦截机制生效；
- `crash_test.py`：双情景 PASS（上表）。

## T8. T-Fin28 多策略赛马与因子接入（2026-10-21 完成）

**四腿结构**（`ACTIVE_LEGS=("A","B","D","E")`，E 为防守腿、不参与赛马排名池排名逻辑见 race.def_floor）：
| 腿 | 策略 | 信号源 | 防守矩阵 |
|---|---|---|---|
| A | 趋势双均线 MA20/60 | 510300 | 黄/红+急跌清仓禁买 |
| B | RSI 均值回归 (14/30/70) | 510300+159915 | 同上 |
| D | 震荡网格 MA120±3% | 510300 | **黄灯放行**（trade 打 grid_yellow），红/急跌清仓禁买 |
| E | 防守红利 510880·MA60 | 独立执行位 | **红灯豁免**（trade 打 defensive），前复权+分红台账 |

- **赛马 race_weights4**：每周五收盘按 120 日夏普排名 → 份额 {t1:.35,t2:.30,t3:.20,t4:.15}；
  max_weekly_shift 0.2、min_weight 0.10、def_floor 0.15（E 保底）。Weekly 频率（业主拍板）。
- **factor_mining 接入**：compute_factors 加 rsi14（单源复用 ensemble_engine.rsi_series）、
  rule_score 加 RSI/ATR 辅助过滤；ATR 高波动罚分仅在 momentum20<0 时生效（上行 breakout 豁免）。
- **510880 分红台账**：params.dividends 10 条（fhsp 在线核验 2017-01-23~2026-01-21，
  每份 0.091~0.143 元）；审计公式 `raw = qfq + Σ{ex>d} per_share` 互证通过。
- **资金恒等式**：eq_total = master_cash + MASTER 停泊份额市值 + Σ腿权益；
  rebalance 注资**先母池后腿**；抽资余数统一回母池（master 是唯一回流位，杜绝腿间环形追讨）。
  旧 2 腿时代漏计 MASTER 停泊份额 无害（master 现金恒 0），四腿后在 run() 对账中补正。

**V 验收**（validate_sim V0–V12 全绿，37 ✅）：
- V3 分腿防守矩阵（D 腿 yellow_ok / E 腿 defensive 台账标记核查）；
- V3f park/redeem 记账行豁免；V3g Σweights == 1−c_buffer；
- V6 现金空转改为**逐账户**口径（任一账户闲置 < 一手货币 ETF 市值）；
- 台账行打标修正：place_orders 返回的 orders 与 port["trades"] 是两份独立 dict，
  leg/defensive/grid_yellow 标记必须打到台账新增行（sleeves[X]["trades"][n0:]）。

**test/audit**：test_sim T28-* 16 项全绿（原 10 项 186 检查保持全绿）；
factor --test 7/7、--audit 8/8（R1=96 R2=0）；risk_control --audit PASS；
黑天鹅 crash_test 双情景 PASS（2015 千股跌停 / 2020 美股熔断，data/crash 含 510880 长历史）。

**策略赛马报告**（`scripts/race_compare.py` → `reports/race_T-Fin28_2026-10-21.md`）：
四腿独立单跑（race.enable=false 单腿满仓）+ 合并赛马 + 2026 年度切片。
2026 结论：基准 510300 买入持有 **−11.35%**；合并赛马净值 **−4.17%**（MDD 4.40%）；
独立单跑 A/B −1.71%（全年持币，急跌门 60 日高点回归未放开）、D −2.95%（黄灯网格 4 笔）、
E −1.71%（防守红利 9 笔）；周赛马夺冠 E=22 次（41 周）。注：单跑口径含 park/redeem
换手磨损（灯色翻转每次 ~2×滑点+费），为既有 T-Fin04 防守设计的诚实代价。
