# -*- coding: utf-8 -*-
"""cloud_probe.py — 凭据自检（T-Fin09 第三轮专用，用户本地网络运行）

用法：python3 scripts/cloud_probe.py <SUPABASE_URL> <SUPABASE_KEY>
自检四处硬伤：
  1. key 是否为完整 JWT 且 anon key 必含 "role":"anon"
  2. JWT ref 与 URL 子域名是否一致（真凭据必须一致）
  3. 本机 DNS 能否解析该子域名
  4. REST 读返回码（200=通，401=key 坏，404=表未建）
退出码：0 全通；1 一致性硬伤（回到网页端重取）；2 网络不可达
"""
import base64, json, socket, sys, urllib.parse

try:
    import requests
except ImportError:
    print("⛔ 缺 requests：pip install requests")
    sys.exit(2)

def die(code):
    sys.exit(code)

if len(sys.argv) != 3:
    print(__doc__); die(2)
url, key = sys.argv[1].rstrip("/"), sys.argv[2]

# ---- 1. JWT 完整性 ----
parts = key.split(".")
if len(parts) != 3:
    print(f"❌ key 不是 JWT（{len(parts)} 段，应为 3 段）"); die(1)
try:
    payload = json.loads(base64.urlsafe_b64decode(
        parts[1] + "=" * (-len(parts[1]) % 4)))
except Exception as e:
    print(f"❌ payload 解码失败：{e}"); die(1)
if payload.get("role") != "anon":
    print(f"❌ payload 缺 role=anon，内容：{payload}\n"
          "   → 不是完好的 Supabase anon key，去 API keys 页面重取"); die(1)

# ---- 2. URL 与 ref 一致性 ----
host = urllib.parse.urlparse(url).hostname or ""
ref = payload.get("ref", "")
sub = host.split(".")[0] if host.endswith(".supabase.co") else ""
print(f"URL 子域 = {sub!r}   JWT ref = {ref!r}")
if sub != ref:
    print("❌ 一致性失败：URL 与 key 不属于同一项目（同一项目二者必须相同）")
    die(1)
print("✅ 一致性 OK")

# ---- 3. DNS ----
try:
    ip = socket.gethostbyname(host)
    print(f"✅ DNS 解析 {host} -> {ip}")
except Exception as e:
    print(f"❌ 本机 DNS 解析失败：{e.__class__.__name__} "
          "（若多次重试仍失败，考虑开代理）"); die(2)

# ---- 4. REST 读 ----
h = {"apikey": key, "Authorization": f"Bearer {key}"}
try:
    r = requests.get(f"{url}/rest/v1/sb_shadow_eod", params={"limit": 1},
                     headers=h, timeout=20)
except Exception as e:
    print(f"❌ 请求异常：{type(e).__name__}: {e}"); die(2)
print(f"HTTP {r.status_code}: {r.text[:200]}")
if r.status_code == 200:
    print("✅ 连接通！若上方返回 [] 则表是空表，跑 sync 之前先在网页端执行建表 SQL 查看行数")
    die(0)
if r.status_code == 404:
    print("⚠️ 项目在线但 sb_shadow_eod 表未建 → 网页端执行 scripts/cloud_schema.sql")
    die(0)
if r.status_code == 401:
    print("❌ 401：key 无效/已被弃用 → API keys 页面重取")
    die(1)
die(1)
