# T-Fin04 · 赛马参数网格寻优（样本外验证报告）

> 生成 2026-10-21 · 网格 16 组 · OOS=最后 63 交易日（未参与训练）· 绝对红线：全历史 MDD ≤ 12%（熔断规则不可被优化）

## 1) 基线（T-Fin03 默认）vs 最优候选

| 口径 | 全程夏普 | 全程MDD | 净值 | 训练夏普 | OOS夏普 | 置信 |
|---|---|---|---|---|---|---|
| 默认参数 | -0.578 | 2.62% | ¥1,008,316 | -0.708 | -16.602 | - |
| 最优候选 | -0.603 | 3.34% | ¥1,005,152 | -0.674 | -16.55 | low |

- 最优参数：`{"race.max_weekly_shift": 0.2, "race.min_weight": 0.4, "A.ma_fast": 10, "A.ma_slow": 60, "B.rsi_buy": 30, "B.rsi_sell": 70}`

## 2) 样本外存活者（Top-6，按 OOS 夏普排序）

| 参数 | 全程MDD | 训练夏普 | OOS夏普 | OOS累计 | 判定 |
|---|---|---|---|---|---|
| `{"race.max_weekly_shift": 0.2, "race.min_weight": 0.4, "A.ma_fast": 10, "A.ma_slow": 60, "B.rsi_buy": 30, "B.rsi_sell": 70}` | 3.34% | -0.674 | -16.55 | -0.0 | ✅通过 |
| `{"race.max_weekly_shift": 0.2, "race.min_weight": 0.4, "A.ma_fast": 20, "A.ma_slow": 60, "B.rsi_buy": 25, "B.rsi_sell": 75}` | 2.54% | -0.81 | -16.564 | -0.0 | ✅通过 |
| `{"race.max_weekly_shift": 0.2, "race.min_weight": 0.4, "A.ma_fast": 10, "A.ma_slow": 60, "B.rsi_buy": 25, "B.rsi_sell": 75}` | 2.71% | -0.79 | -16.571 | -0.0 | ✅通过 |
| `{"race.max_weekly_shift": 0.2, "race.min_weight": 0.3, "A.ma_fast": 10, "A.ma_slow": 60, "B.rsi_buy": 30, "B.rsi_sell": 70}` | 2.73% | -0.692 | -16.594 | -0.0 | ✅通过 |
| `{"race.max_weekly_shift": 0.1, "race.min_weight": 0.3, "A.ma_fast": 10, "A.ma_slow": 60, "B.rsi_buy": 30, "B.rsi_sell": 70}` | 2.80% | -0.679 | -16.597 | -0.0 | ✅通过 |
| `{"race.max_weekly_shift": 0.2, "race.min_weight": 0.3, "A.ma_fast": 20, "A.ma_slow": 60, "B.rsi_buy": 30, "B.rsi_sell": 70}` | 2.62% | -0.708 | -16.602 | -0.0 | ✅通过 |

## 3) 防过拟合结论

- MDD 红线淘汰 0 组；最终置信 **low**（OOS 夏普崩塌>50% 或 ≤0 → 系统已拦截（防过拟合））
- 规则：OOS 夏普较训练期下跌超 50% 或 OOS ≤ 0 → 拦截、confidence=low，
  参数仍写入台账但**不会自动生效**（需 CRO 人工确认后手工改 params.yaml）。
- 排序依据 OOS 夏普（样本外表现优先于训练集过拟合高分）。

## 4) 台账溯源

- 建议值：`index/ensemble_suggested.json`；审计流水：`data/optimizer_log.jsonl`；
- 网格维度：`race.max_weekly_shift / race.min_weight / A.ma_fast·ma_slow / B.rsi_buy·rsi_sell`
  定义于 `scripts/optimizer.py ENSEMBLE_GRID`（台账登记小范围搜索，非暴力全域）。

