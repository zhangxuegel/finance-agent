# finance-agent — T-Fin01 本地量化交易 Agent

纯本地**模拟盘**（初始 100 万元虚拟资金），铁律见 `AGENTS.md`：
LLM 只当策略分析师/风控官，信号一律由确定性公式生成，禁止接实盘。

## 快速开始

```bash
python3 scripts/fetch_data.py          # Task1: 抓取近3年日线 → data/*.csv（校验覆盖/缺口）
python3 scripts/test_sim.py            # 自检：费用/滑点/估值恒等式/熔断（全过才是绿灯）
python3 scripts/daily_report.py --backtest   # 全历史回测一次看看策略复制效果
python3 scripts/daily_report.py --replay 20  # 重放最近 20 个交易日（每日一份复盘报告）
python3 scripts/daily_report.py        # 日常增量推进 1 个交易日并出报告
python3 scripts/optimizer.py           # 每周网格搜索 + 样本外拦截（防过拟合）
```

## 分阶段交付（任务书对应关系）

| Task | 交付文件 | 验收状况 |
|---|---|---|
| 🟢 T1 数据基建 | `index/params.yaml`（费率台账+来源）、`index/watchlist.yaml`、`scripts/fetch_data.py` | 4 标的×725 行、覆盖 100%、无 >12 自然日缺口 |
| 🟡 T2 策略引擎 | `scripts/strategy.py`（A 双均线 / B 布林带） | 仅输出 0/1 信号，不碰资金 |
| 🟠 T3 风控账户 | `scripts/risk_control.py`、`scripts/portfolio.py` | 单日-3%/峰值-10% 熔断+锁3日，next-open 撮合+佣金/印花税/滑点，test_sim 15 项全过 |
| 🔴 T4 AI晨报 | `scripts/daily_report.py` | 每日 `reports/<date>.md`，含交易指令/持仓/绩效看板/LLM(或规则点评)分析/数据溯源 |
| 🟣 T5 进化引擎 | `scripts/optimizer.py` | 75%/25% 训练-样本外切分；OOS 崩塌自动拦截 `confidence: low` |

## 本次 3 年全历史回测快照（策略 A，含风控真实生效）

- 净值 **¥1,521,532**（累计 +52.15%，年化 +15.66%）
- 最大回撤 **9.73%**（< 10% 风控线 ✅ "优"档）
- 夏普比率 **1.084**（> 1.0 ✅）
- 胜率 **41.3%** / 92 笔（未达 45% 参考线，待策略 B 或 optimizer 调参改善）
- 期间熔断真实触发 1 次，锁仓/强平链路验证通过

## 台账/状态文件

```
index/params.yaml          本金/佣金/印花税/滑点/策略/熔断/LLM 参数（唯一真源）
index/watchlist.yaml       股票池（真实代码 + 数据源）
index/params_suggested.json   优化器最新建议（confidence: high/low）
index/optimizer_log.jsonl  优化历史台账（可追溯）
data/*.csv                 行情（东方财富主源 / 腾讯备源，免 Token）
data/portfolio.json        账户状态（现金/持仓/流水/净值曲线）
data/risk_state.json       风控锁仓状态 + 熔断事件簿
reports/<date>.md          每日复盘
```

## 定时运行（模拟盘每日收盘后）

```bash
# crontab -e（收盘后 18:05 抓数据 → 出报告）
5 18 * * 1-5  cd ~/finance-agent && python3 scripts/fetch_data.py >> data/cron.log 2>&1
10 18 * * 1-5 cd ~/finance-agent && python3 scripts/daily_report.py >> data/cron.log 2>&1
# 周六 12:00 跑一次进化引擎
0 12 * * 6    cd ~/finance-agent && python3 scripts/optimizer.py >> data/cron.log 2>&1
```

## 概念图谱（T-Fin01 ↔ T-Fin02 ↔ T-Fin03 挂接表，0 断链）

| 概念 | 挂接点 | 台账/参数 |
|---|---|---|
| 防未来函数时间锁（信号口径） | `risk_control.audit_no_lookahead`（截断重算法×40 探针） | `stress.lookahead_probes` |
| 时间锁（撮合口径） | `risk_control.audit_fill_time_lock`（账本逐笔：买=开×(1+slip)、卖=开×(1-slip)、信号日=成交前一交易日） | `fees.slippage_bps` |
| 高滑点地狱模式 | `risk_control.stress_high_slippage`（滑点×N 全历史重放+生存判定） | `stress.slippage_multiplier` |
| 大盘信号灯 绿/黄/红 | `regime_filter.traffic_light`（close×MA60×MA120 排列法）→ 每日报告 0 区 + CRO 语境 | `benchmark.*` |
| Sortino / Calmar | `portfolio.metrics` → 绩效看板 | —— |
| 首席风控官 Prompt | `daily_report.ANALYST_PROMPT`（CRO 四问，敞口数字系统强制注入防幻觉） | `llm.*` |
| **三腿赛马引擎** | `ensemble_engine.EnsembleEngine`（A双均线/B RSI/C货币腿）+ `race_weights` | `ensemble.*` |
| **黄/红灯状态机** | `ensemble_engine.assert_gate` → FatalControlError（黄/红日 BUY 即终止） | —— |
| **平滑防过拟合** | `race_weights` 单周漂移 ≤±20pp + 双方权重下限 0.3 | `ensemble.race.*` |
| **C 腿货币防线** | `park_money/redeem_money` → 511990（不留现金空转） | `ensemble.C` / watchlist |
| **LLM 因子辅助调权** | `factor_mining.llm_score`（解析→50 降级；超界→clip；只进 tilt 位不生成订单） | `factor.*` + `ensemble.tilt.*` |
| **每日推送** | `push_notification`（摘要=系统台账数字；mock 闭环；双保险防误发） | `push.*` |
| **定时闭环** | `run_daily_all.sh` + crontab `30 15 * * 1-5` | `cron.*` |
| **赛马参数寻优（OOS 拦截）** | `optimizer.run_ensemble`（16 组网格；OOS=最后 63 交易日；OOS 夏普崩塌>50%/≤0 → 拦截降置信） | `ensemble_suggested.json` |
| **因子分硬逻辑审计** | `factor_mining.audit_factor_logic`（极端牛/熊行情 → 分数常识校验；违背常识抛 FactorBiasError、记录 `LLM_FACTOR_BIAS`、硬回退规则分） | `data/factor_bias_log.jsonl` |
| **晨报/周末深度复盘** | `report_generator --daily/--weekly`（晨报=灯色/可用资金/持仓分布/昨日盈亏；周报=三腿对比/权重演变/CRO 总结）+ **台账溯源**尾节 | `cron.morning_schedule` |
| **急跌熔断门** | `ensemble_engine`（基准/逐资产 防守出闸三条件：灯绿+近5日无急跌+高点回归；观察期买入预算衰减 50%） | `ensemble.crash_gate.*` |
| **黑天鹅崩溃测试** | `crash_test.py`（2015 千股跌停 & 2020 美股熔断 真实历史 × 完整系统；避险 ≤2 交易日、MDD ≤5%，失败自动链路推演） | `data/crash/` 长历史隔离落盘 |

审计执行：`python3 scripts/risk_control.py --audit` → `reports/risk_audit_<date>.md`
赛马回测：`python3 scripts/ensemble_engine.py --daily --replay 20` → `reports/ensemble_<date>.md`
因子兜底自检：`python3 scripts/factor_mining.py --test`；推链路自检：`python3 scripts/push_notification.py --test`
黑天鹅拷问：`python3 scripts/crash_test.py` → `reports/crash_test_<date>.md`（先 `python3 scripts/factor_mining.py --audit` 验因子常识）
每组寻优：`python3 scripts/optimizer.py --ensemble` → `reports/optimizer_ensemble_<date>.md`
验收自检：`python3 scripts/validate_sim.py`（T-Fin03/04 合并）+ `test_sim.py`（T-Fin01/02 回归）

详见 `docs/ensemble_and_factor.md`。

## LLM 接入说明

`params.yaml → llm.*` 已预置 GLM OpenAI 兼容接口；若环境未设 `GLM_API_KEY`，
报告自动降级为"规则点评"（回撤/胜率/健康度评分），链路不中断。LLM 无论是否收到
API 响应，都**只能写文字分析区，不产生任何买卖指令**。

风险提示：本项目仅用于学习与研究，不构成任何投资建议。
