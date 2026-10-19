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
