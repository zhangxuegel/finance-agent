# AGENTS.md — Agent 启动铁律（T-Fin01）

本目录是**本地模拟盘**量化交易 Agent 的工作区（`.workdir` 已锁死根目录）。

## 铁律（违反任何一条即为事故）

1. **模拟资金 100 万元，绝对禁止接入实盘、券商账户或任何真实交易 API。**
2. **数据台账化**：所有行情、手续费、滑点参数必须能在 `index/params.yaml`
   与 `index/watchlist.yaml` 中找到登记项及来源说明。禁止任何脚本
   （包括 LLM 自身）凭空生成行情数字——一切数字必须来自
   `scripts/fetch_data.py` 从公开行情接口拉取并落盘的 CSV。
3. **风控最高优先级**：任何策略都必须叠加上
   `scripts/risk_control.py` 的两条熔断规则
   （单日亏损 > 3% 强制清仓锁 3 天；净值自峰值回撤 > 10% 强制清仓锁 3 天）。
   熔断规则是绝对规则，optimizer 不得优化掉它。
4. **LLM 只当"策略分析师"和"风控官"，绝不下单。**
   买卖信号一律由 `scripts/strategy.py` 的确定性数学公式生成；
   LLM 的输出只能出现在 `reports/` 的文字分析区。
5. 交易指令 = 信号日收盘生成，**次日以开盘价撮合**，附加滑点与手续费，
   由 `scripts/portfolio.py` 统一记账，其他模块不得私改账本。

## 目录约定

```text
index/    台账（params.yaml / watchlist.yaml）
scripts/  全部可执行逻辑；模块间只允许 import，不允许互相改文件
data/     行情 CSV、持仓状态 data/portfolio.json、风控状态 data/risk_state.json
reports/  每日 Markdown 复盘报告
```

## 每日运行

```bash
python3 scripts/fetch_data.py      # 收盘后更新 data/*.csv
python3 scripts/daily_report.py    # 越日推进 + 生成 reports/<date>.md
```
