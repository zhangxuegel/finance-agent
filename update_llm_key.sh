#!/data/data/com.termux/files/usr/bin/sh
# update_llm_key.sh — 交互式更新 GLM_API_KEY
# 设计：在你自己的终端里运行；输入时不回显，脚本输出也不显示 key 本体。
# 职责：仅维护 ~/.bashrc 导出行 + crontab 环境头部。不触碰 scripts/ 交易逻辑。
# 用法：bash ~/finance-agent/update_llm_key.sh

set -eu

printf '粘贴新 GLM_API_KEY 后按回车（输入不回显）：\n> '
stty -echo 2>/dev/null || true
IFS= read -r KEY
stty echo 2>/dev/null || true
printf '\n'

if [ -z "${KEY:-}" ]; then
  echo '❌ 未输入内容，已取消（未做任何修改）。'
  exit 1
fi

# 轻格式提示（不打印 key 本体；智谱 key 常见形如 id.secret）
case "$KEY" in
  *.*) ;;
  *) echo '[提示] 输入与常见智谱格式 id.secret 不符，仍按原样写入。' ;;
esac

# 1) ~/.bashrc：清掉旧键行，只保留最新一条
BRC="$HOME/.bashrc"
touch "$BRC"
TMP="$BRC.tmp.$$"
{ grep -v 'export GLM_API_KEY=' "$BRC" || true; } > "$TMP"
cat >> "$TMP" <<EOF
# === finance-agent (T-Fin) LLM 注入（由 update_llm_key.sh 维护） ===
export GLM_API_KEY="$KEY"
EOF
mv "$TMP" "$BRC"

# 2) crontab 环境头部：替换 GLM_API_KEY= 行；无 crontab 时按台账默认计划生成
if command -v crontab >/dev/null 2>&1; then
  CTMP="$HOME/.cron_new.$$"
  if crontab -l >/dev/null 2>&1; then
    { crontab -l | grep -v '^GLM_API_KEY=' || true; } > "$CTMP"
    sed -i "1i GLM_API_KEY=$KEY" "$CTMP"
  else
    printf 'GLM_API_KEY=%s\n\n30 15 * * 1-5 cd $HOME/finance-agent && sh scripts/run_daily_all.sh >> data/cron.log 2>&1\n' "$KEY" > "$CTMP"
  fi
  crontab "$CTMP"
  rm -f "$CTMP"
  cron_done=yes
else
  cron_done=no
fi

# 3) 校验回执：只报长度，不显示 key 本体
LEN=$(printf %s "$KEY" | wc -c)
echo "✅ 已更新 ~/.bashrc 导出行（key 长度 $LEN 字符）"
if [ "$cron_done" = "yes" ]; then
  echo "✅ 已更新 crontab 头部环境变量（crond 自动生效，无需重启）"
else
  echo "[警告] 未检测到 crontab，cron 头部未写入"
fi
echo "→ 当前终端立即生效，请执行：source ~/.bashrc"
