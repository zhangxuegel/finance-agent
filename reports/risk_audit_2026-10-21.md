# T-Fin02 风控综合审计报告
> 生成时间 2026-10-21 17:46:30 · 台账口径：index/params.yaml

## 1) 防未来函数时间锁校验（信号口径）

- 方法：截断重算法，探针日 ×40（台账 stress.lookahead_probes）
- 510300：40 探针，✔ 全部一致（无未来函数）
- 159915：40 探针，✔ 全部一致（无未来函数）
- 513500：40 探针，✔ 全部一致（无未来函数）
- 518880：40 探针，✔ 全部一致（无未来函数）
- 511990：40 探针，✔ 全部一致（无未来函数）
- 510880：40 探针，✔ 全部一致（无未来函数）

**结论：✔ PASS 全库存无未来函数**

## 2) 撮合口径时间锁（账本逐笔核对）

- 核对成交 186 笔：买价=开×(1+slip)、卖价=开×(1-slip)、信号日=成交前一交易日、强平=收×(1-slip)（forced 标记豁免信号检查）
**结论：✔ PASS 账本与时间锁完全一致**

## 3) 高滑点地狱模式回测

- 滑点倍数 ×10（台账 stress.slippage_multiplier）
- 基准回测：NAV ¥1,538,758，MDD 9.89%，夏普 1.108，Sortino 1.77，Calmar 1.627
- 地狱回测：NAV ¥1,010,462，MDD 19.03%，夏普 -0.063，Sortino -0.182，Calmar 0.019
**结论：⚠ 未存活：对滑点冲击过敏感**

## 4) 概念图谱挂接（T-Fin01 ↔ T-Fin02）

| 概念 | 挂接点 |
|---|---|
| 防未来函数时间锁 | risk_control.audit_no_lookahead + audit_fill_time_lock |
| 高滑点地狱模式 | params.stress.slippage_multiplier → risk_control.stress_high_slippage |
| 大盘信号灯 | regime_filter.traffic_light → daily_report 报告 0 区 |
| Sortino / Calmar | portfolio.metrics → daily_report 绩效看板 |
| 首席风控官 Prompt | daily_report.ANALYST_PROMPT + 敞口快照强制注入 |

- 声明：本报告为模拟盘学习用途，不构成投资建议。
