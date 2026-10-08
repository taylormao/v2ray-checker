# 节点检测工具 v2.0（内核验证版）

在1.0.0 基础上重构：核心变化是**引入真实代理内核做端到端验证**，消除 1.0 版本的
大面积假阳性。

---

## 为什么必须升级

1.0 版的检测逻辑只有两级（见 `check_subscription.py:279-335`）：

```python
socket.create_connection((host, port))   # L2 端口通不通
ssl.wrap_socket(...)                     # L3 TLS 握手
```

**它从来没真正跑过代理。** 所以只能证明「端口开着」+「有 TLS 服务」，
无法证明这是个能用的节点。

实测交叉验证（拿 1.0 版判定"有效"的 33 个节点跑2.0）：

| | 数量 | 说明 |
|---|---|---|
| 1.0 版判定"有效" | 33 | TCP + TLS 能通 |
| **2.0 真实可用** | **1** | 通过内核端到端验证 |
| 隧道握手被拒 | 11 | 端口开着，但 VLESS/WS 握手失败（节点已失效） |
| 隧道建了但出不去 | 6 | 握手成功，但目标站无响应（出口被墙） |
| TCP 不通 | 13 | 上一轮抖动，本轮直接失败 |

**假阳性率 97%** —— 1.0 版的检测结果基本等于随机数。

---

## 五级漏斗

```
① 解析有效性   ~0ms      必填字段、uuid 格式、TLS 与 SNI 配套
② TCP 连通      ~4s       砍掉端口不通
③ TLS 握手      ~5s       砍掉证书/SNI 问题
④ ★ 内核端到端  ~8s       唯一权威判据
⑤ 隧道测速      ~4-6s     只对 TOP N 跑
```

前三级便宜，能砍掉绝大多数节点；只有存活节点才进入昂贵的内核级检测。
33 个节点全流程约 20 秒。

### 为什么第 ④ 级是唯一权威判据

只有「**通过节点自己的隧道，拿到目标站返回的真实 HTTP 响应**」才能证明能用。
判据是 `www.gstatic.com/generate_204` 返回 **HTTP 204**。

它能识别的假阳性，原工具全部识别不出：

| 假阳性 | 1.0 判定 | 2.0 判定 |
|---|---|---|
| 端口开放但不是代理服务 | 有效 | ❌ 隧道握手被拒 |
| VLESS uuid / flow 填错 | 有效 | ❌ 握手被拒 |
| SNI / host / path 不匹配 | 有效 | ❌ 握手被拒或路由失败 |
| 能连但**墙内访问不了目标站** | 有效 | ❌ 目标无响应 |
| 节点服务返回 301/302 重定向 | 有效 | ❌ unexpected HTTP response status |

最后一种在实际输出里精确显示为：
```
ERROR connection: open connection to www.gstatic.com:80 using outbound/vless[proxy]:
unexpected HTTP response status: 301
```

---

## 用法

```bash
# 从订阅链接
python -m v2ray_checker_1_0_0.cli --url <订阅URL>

# 从文件
python -m v2ray_checker_1_0_0.cli --file nodes.txt

# 管道输入
cat nodes.txt | python -m v2ray_checker_1_0_0.cli --paste

# 只跑到 L3（快，但仍有假阳性）
python -m v2ray_checker_1_0_0.cli --file nodes.txt --no-kernel

# 带测速（只测 TOP 10）
python -m v2ray_checker_1_0_0.cli --file nodes.txt --speed --top 10
```

### 输出

| 文件 | 用途 |
|---|---|
| `result/sub_<时间戳>.txt` | **base64 订阅**，v2rayN 直接导入，备注含评分 |
| `result/valid_<时间戳>.txt` | 明文 URI 列表，便于查看粘贴 |
| `result/report_<时间戳>.json` | 结构化报告：漏斗各级淘汰数、失败原因分布、每个节点全指标 |

---

## 评分规则

刻意**不把延迟当主导因子** —— 国内网络环境下，
「延迟低但丢包/抖动大」的节点体感远差于「延迟略高但稳定快速」的节点。

| 维度 | 权重 | 说明 |
|---|---|---|
| **稳定性** | 45% | 由延迟 + **抖动**共同决定。抖动按 `min(30, jitter×0.5)` 单独扣分 |
| **速度** | 30% | 对数刻度，1Mbps→10Mbps 的差距远小于 10→50 |
| **延迟** | 20% | |
| 落地机房 | 5% | 仅记录，不实质影响分数 |

实测验证抖动惩罚的效果：同一个节点 `287ms ±68ms` 时，
评分从 50 降到 **36** —— 它延迟不高但抖动大，体感会卡。

---

## ⚠️ 测速的结构性约束（重要）

公共测速站（`speed.cloudflare.com` / `cachefly` / `proof.ovh` /
`speedtest.tele2`）在国内**全都需要走代理**，而节点出口未必通它们。

实测：大量节点对**所有测速站**均返回 `SOCKS5 CONNECT rep=1`，
但 `generate_204` 却能过 —— 说明它们能连小响应，下不了大文件。

因此测速做成两条路径：

| 路径 | 条件 |产出 |
|---|---|---|
| A. 带宽测量 | 某个测速站可达 | 真实 Mbps，`speed_mode="bandwidth"` |
| B. **降级：RTT 探测** | 测速站全不通 | 平均/最小/最大延迟 + **抖动标准差**，`speed_mode="rtt"` |

降级路径用 `generate_204` 连发 6 次算标准差。**不给 Mbps，但给抖动** ——
而抖动恰恰是体感流畅度的关键指标，比一个假的 0 Mbps 有意义得多。

---

## ⚠️ 订阅拉取的代理陷阱

`--proxy` 默认 `http://127.0.0.1:10808`（Clash），**不要依赖环境变量**：

本机环境的 `https_proxy` 常被 WorkBuddy 之类工具设成白名单代理
（如 `127.0.0.1:8426`），它只放行部分域名。访问 GitHub Raw 会报：

```
Tunnel connection failed: 502 Bad Gateway
```

而 Clash 在 10808，完全正常。所以 `fetch_subscription` 的策略是
**显式代理 → 环境代理 → 直连**，三级依次尝试，全失败才抛错。
用 `--proxy none` 可强制直连（适合国内 CDN 源）。

---

## 依赖

```
bin/sing-box.exe   1.14.2   ← 默认，自动探测
bin/xray.exe26.3.27   ← 备用
```

两者已下载到 `bin/`。自动探测顺序：sing-box → xray。
内核找不到时会明确报错并回退到 L3 模式，**不会静默降级**。

### ⚠️ sing-box 1.14 的三个坑（都踩过）

**① 传输层必须用统一`transport` 字段**

```jsonc
// ❌ 1.13+ 报 json: unknown field "ws"
{"type": "vless", "ws": {"path": "/", "headers": {"Host": "a.com"}}}

// ✅ 1.11+ 统一写法
{"type": "vless", "transport": {"type": "ws", "path": "/", "headers": {"Host": "a.com"}}}
```

**② 裸 TCP 不能写 transport**

```jsonc
{"type": "vless"}                                          // ✅
{"type": "vless", "transport": {"type": "tcp"}}            // ❌ unknown transport type: tcp
```

**③ QUIC 类与 shadowsocks 不支持 transport 字段**

`hysteria2` / `tuic` / `wireguard` / `ss` 加上 `transport` 会报
`unknown field "transport"` —— 它们不走 vless/vmess 的传输抽象。

其他版本差异：
- `inbounds` 的旧字段（`sniff` 等）已被 `route.rules[].action` 取代，
  1.13+ 直接 FATAL
- 协议名是 `shadowsocks` 不是 `ss`

---

## ⚠️ SOCKS5 客户端实现注意

`kernel_probe._http_via_socks` 手工实现了 SOCKS5 握手，两个易错点：

**① CONNECT 之后不能复用 http.client**

`conn.request("CONNECT", ...)` 之后 socket 所有权已交给隧道，
再调 `conn.request()` 会抛 `Bad file descriptor` 或静默发不出去。
必须手工发 SOCKS5 握手，再用裸 socket 发 HTTP 报文。

**② BND.ADDR 必须按 ATYP 精确读取**

写错会连带把后面的字节当端口 —— 曾表现为「连到 `0.0.0.0:18`」。

**③ 端口必须真实编码**

```python
req += bytes([len(host)]) + host + b"\x00\x00"   # ❌ 连到 host:0
req += bytes([len(host)]) + host + port.to_bytes(2, "big")   # ✅
```

---

## 文件说明

```
src/v2ray_checker_1_0_0/
├── kernel_probe.py    内核探测、配置生成、SOCKS5 客户端、端到端验证、测速
├── pipeline.py        NodeSpec 数据模型、五级漏斗、评分排序、输出
└── cli.py             命令行入口 + 多协议 URI 解析

bin/                  内核二进制（sing-box / xray）
```

1.0 版的 `check_subscription.py` 与 `app.py`（Flask Web UI）保持原样未动，
可作为对照与回退。

---

## 已知限制

| 限制 | 说明 |
|---|---|
| **内核检测需要下载二进制** | 约 50MB（含 geoip/geosite 数据库） |
| **QUIC 类协议未做真实验证** | hysteria2/tuic 的 transport 行为与 vless 不同，当前仅做配置生成，未跑通端到端 |
| **未改Web UI** | 2.0 目前是 CLI。Flask 界面调`pipeline.run_funnel` 即可接入，但 UI 未改造 |
| **测速目标固定** | 用 `speed.cloudflare.com/__down`，未做多目标容错 |

---

# Web UI（app_v2.py）

## 启动

```bash
双击 start_v2.bat          # 或
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe app_v2.py
# 浏览器打开 http://localhost:5000
```

停止：`stop_v2.bat`（或 `start.bat` 停旧版）。

## 与 1.0 的差异

| | 1.0（app.py） | 2.0（app_v2.py） |
|---|---|---|
| 检测方式 | subprocess 拉 `check_subscription.py`，解析 stdout 文本 | **进程内直接调 `run_funnel_green`** |
| 判据 | TCP + TLS | 真实内核建隧道，拿 `generate_204` 的 204 |
| 进度 | 只有「已检测 N个」 | 阶段化（L1→L2/L3→L4）+ 实时百分比 |
| 结果展示 | 仅结果文件列表 | **KPI 卡 + TOP 排行表 + 失败原因分布条** |
| 停止 | kill 子进程 | eventlet Event 协作式中止 |

## 新增 UI

**检测设置区**多了三个控件：

| 控件 | id | 默认 | 说明 |
|---|---|---|---|
| 启用隧道测速 | `speedCheck` | 关| 只对 TOP N 跑，最慢 |
| 测速节点数 | `topN` | 10 | |
| 订阅拉取代理 | `proxyInput` | `http://127.0.0.1:10808` | 填 `none` 强制直连 |

**结果区**顶部新增「内核验证面板」：

- 6 张KPI 卡：解析节点 / TCP 通 / TLS 通 / ★真实可用 / 真实可用率 / 验证内核
- TOP 排行表：排名、节点、类型、评分、延迟、抖动、速度
- 失败原因分布：横向条形图，一眼看清「端口通但不能用」占多少

## 新增接口

| 接口 | 说明 |
|---|---|
| `GET /api/results` | **结构化结果**（面板数据）。v2 改动过契约 —— 1.0 返回文件数组 |
| `GET /api/result_files` | 结果文件列表（下载用）。把两个关注点拆开 |
| `POST /api/stop` | 返回 `{success, message}`（1.0 是 `{status:"stopped"}`） |

前端 `refreshResults()` 已做双契约兼容，两个版本都能用。

## ⚠️ eventlet 适配（这是移植最大的坑）

原`cli.run_funnel` 用 `ThreadPoolExecutor`，在 eventlet 下**跑不起来**。
新增 `src/v2ray_checker_1_0_0/web_funnel.py` 用绿色线程重写：

```python
from eventlet import Event, GreenPool, Semaphore, spawn
```

实测踩了五个坑（已沉淀为技能 `eventlet-flask-migration`）：

|坑 | 症状 | 解法 |
|---|---|---|
| 从 `eventlet.green.thread` 导入 | `ImportError: cannot import name 'spawn'` | 从 `eventlet` 顶层导入 |
| `Event.clear()` 不存在 | 服务崩在 `reset_status` | 换新 `Event()` 实例 |
| `Event.set()` / `is_set()` 不存在 | **节点静默停在 L3**（异常被吞） | 用 `send()` / `ready()` |
| `Semaphore` 死锁| 进度条不动 | 一次 spawn 全部，信号量在函数内限流 |
| `counter[0] += 1` 竞态 | 漏斗统计错乱 | 加锁保护计数 |

其中「节点静默停在 L3」最隐蔽 —— 异常被 `except Exception: pass` 吞掉，
表现像是并发问题，实际是API 用错。排查时把 except 改成打印堆栈才暴露。

**阻塞子进程**也会冻结服务（eventlet 单线程协作调度），
`_wait_port` 里改用 `eventlet.sleep(0.1)` 让出 CPU。

## 兼容性

- 1.0 的 `app.py` / `check_subscription.py` / `index.html.bak` 全部保留
- `templates/index.html` 是在原版上**增量修改**（备份在 `index.html.v2bak`）
- 两个版本可分别启动，端口都是 5000，注意别同时跑
