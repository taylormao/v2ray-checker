# 开发文档

面向二次开发者。读本文前建议先看 `../README.md` 了解工具定位。

---

## 架构

### 分层

```
┌─────────────────────────────────────────────────────┐
│  入口层                                              │
│  cli.py            app_v2.py（Flask+Socket.IO）      │
└───────────────┬─────────────────────────────────────┘
                │
┌───────────────▼─────────────────────────────────────┐
│  漏斗层（本仓库有两份实现，见「双漏斗」章节）        │
│  cli.run_funnel              web_funnel.             │
│  （ThreadPoolExecutor）      run_funnel_green        │
│                               （eventlet 绿线程）    │
└───────────────┬─────────────────────────────────────┘
                │
┌───────────────▼─────────────────────────────────────┐
│  核心层                                             │
│  pipeline.py     数据模型 / 校验 / 评分 / 输出       │
│  kernel_probe.py 内核探测 / 配置生成 / 端到端验证     │
└───────────────┬─────────────────────────────────────┘
                │
┌───────────────▼─────────────────────────────────────┐
│  外部进程                                          │
│  bin/sing-box.exe 或 bin/xray.exe                   │
└─────────────────────────────────────────────────────┘
```

### 关键设计：为什么不自己实现代理协议

判断节点能否上网，需要完成「代理协议握手 + 目标请求」。这部分逻辑复杂且易错
（VLESS 的 flow、Xray Vision、REALITY 等等），自己实现等于重写内核。

所以采用**委托策略**：

1. 把节点参数翻译成 sing-box / xray 的配置 JSON
2. 起内核实例，监听本地随机 SOCKS 端口
3. 自己手工做 SOCKS5 握手，发真实 HTTP 报文
4. 判定响应，关闭实例

我们只实现「配置生成」+「SOCKS5 客户端」，**不碰代理协议本身**。
收益：协议支持广（内核支持什么就支持什么）、维护成本低。

代价：每个节点要起一个进程（约 5~8 秒），比自实现慢。
所以必须用漏斗把廉价的前置检查放前面。

---

## 双漏斗：为什么有两份实现

| | `cli.run_funnel` | `web_funnel.run_funnel_green` |
|---|---|---|
| 并发原语 | `ThreadPoolExecutor` | `eventlet.Semaphore` + `spawn` |
| 用于 | CLI | Web UI |
| 推进度 | `as_completed` | 自实现 `_parallel_map` |

**为什么不能共用**：`app_v2.py` 运行在 eventlet 下（Flask-SocketIO 需要）。
eventlet 已monkey_patch 掉 `threading`，`ThreadPoolExecutor` 会被改成绿色线程池，
而 L4 的内核检测是阻塞 IO（每个 5~8 秒），用绿色线程做阻塞等于**串行**，
表现为界面长时间无响应。

`web_funnel` 用 `Semaphore` 做真并发，且实现「完成即回调」——
`GreenPool` 只有**有序**的 `imap`（会阻塞等结果），没有 `imap_unordered`。

### `_parallel_map` 的两个致命坑

```python
#⚠️ 坑1：不能「主线程串行 acquire → 再 spawn」
threads = []
for n in items:
    sem.acquire()          # ← 前 size 个占满后死锁
    threads.append(spawn(runner, n))
# 死锁原因：等 acquire 释放，但释放依赖已被 spawn 的任务跑完，
#           任务却还没被 spawn → 循环等待

# ✅ 一次 spawn 全部，由信号量在函数内部限流
threads = [spawn(runner, n) for n in items]

# ⚠️ 坑2：counter[0] += 1 不是原子操作
# 协程会在 += 中间被切换，导致丢更新，all_done 永不触发 → 永久挂起
counter[0] += 1

# ✅ 加锁
with clk:
    counter[0] += 1
    if counter[0] >= total:
        all_done.send()
```

两个坑的症状都是「进度条不动」，但根因不同：
**全停在同一层 = 死锁；层间分布乱 = 计数丢失。**

---

## eventlet 适配要点

Web UI 相关的坑已沉淀为独立技能，这里列最关键的：

| 坑 | 症状 | 解法 |
|---|---|---|
| 从 `eventlet.green.thread` 导入 | `ImportError: cannot import 'spawn'` | 从 `eventlet` 顶层导入 |
| `Event.clear()` 不存在 | 服务崩在 `reset_status` | 换新 `Event()` 实例 |
| `Event.set()`/`is_set()` 不存在 | **节点静默停在 L3** | 用 `send()`/`ready()` |
| 阻塞子进程调用 | 整个服务冻结 | `_wait_port` 里用 `eventlet.sleep(0.1)` |
| `socketio.run(allow_unsafe_werkzeug=...)` | `TypeError` | 该参数只属 werkzeug 后端 |

其中「节点静默停在 L3」最隐蔽：异常被 `except Exception: pass` 吞掉，
表现像是并发问题，实际是 API 用错。
**排查手法：临时把 except 改成 `traceback.print_exc()`。**

---

## 前端契约

复用 1.0 的 `templates/index.html` 增量改造，**必须逐个核对三处契约**：

| 契约 | 1.0 | 2.0 兼容写法 |
|---|---|---|
| `log` 事件载荷 | `{timestamp, message, level}` | **必须带 `timestamp`**（前端 `addLog` 是三参数签名，缺了渲染成 `undefined`） |
| `/api/*` 响应 | `{error}` / `{status}` | `{success, message}` → 前端写成 `data.success === false \|\| data.error` |
| 回调参数顺序 | `addLog(ts, msg, level)` | 注意别把 `'[系统]'` 当 timestamp 传 |

**这三类最容易出现「后端逻辑对但界面不出东西」的静默失败。**

### 接口变更

| 接口 | 1.0 | 2.0 |
|---|---|---|
| `GET /api/results` | 文件数组 | **结构化面板数据** |
| `GET /api/result_files` | — | 新增，文件列表 |
| `POST /api/stop` | `{status:"stopped"}` | `{success, message}` |

拆成两个接口是因为「面板数据」与「文件下载」是两个不同关注点。

---

## 核心实现要点

### sing-box 配置生成的三个坑

1. **`type` 会被 `**` 展开覆盖**

```python
def _ws_opts(node):
    out = {"type": node.network}    # ❌ 调用方用 ** 展开，会把 vless 写成 ws
    ...
def build_config(node):
    outbound = {"type": node.type, ..., **_ws_opts(node)}   # ❌ vless 被覆盖
```

`_ws_opts` 必须只返回传输层字段，绝不能含 `type`。

2. **1.14 用统一 `transport` 字段**

```jsonc
// ❌ 1.13+ FATAL
{"type":"vless", "ws": {"path":"/", "headers":{"Host":"a.com"}}}
// ✅ 1.11+
{"type":"vless", "transport": {"type":"ws", "path":"/", "headers":{"Host":"a.com"}}}
```

3. **协议各自的例外**

```jsonc
{"type":"vless"}                                       // ✅ 裸 TCP 省略 transport
{"type":"vless", "transport":{"type":"tcp"}}           // ❌ unknown transport type
{"type":"hysteria2", "password":"x"}                   // ✅ 不写 transport
{"type":"shadowsocks", "password":"x", "method":"..."} // ✅ 不写 transport，type 必须是 shadowsocks
```

### SOCKS5 客户端的三个坑

`kernel_probe._http_via_socks` 手工实现 SOCKS5，三个必踩点：

**① CONNECT 后不能复用 `http.client`**

```python
# ❌ socket 所有权已交给隧道，第二次 request() 会抛 Bad file descriptor
conn.request("CONNECT", f"{host}:{port}")
conn.request("GET", path)

# ✅ 手工握手 + 裸 socket 发报文
sock.sendall(b"\x05\x01\x00")   # 方法协商
...# CONNECT + 读响应
sock.sendall(b"GET / HTTP/1.1\r\nHost: ...\r\n\r\n")
```

**② BND.ADDR 必须按 ATYP 精确读取**

写错会连带把后面的字节当端口 —— 症状是「连到 `0.0.0.0:18`」：

```python
if atyp == 0x01:   _recv_exact(sock, 4 + 2)      # IPv4
elif atyp == 0x03:                                # 域名
    ln = _recv_exact(sock, 1)
    _recv_exact(sock, ln[0] + 2)
elif atyp == 0x04: _recv_exact(sock, 16 + 2)      # IPv6
```

**③ 端口必须真实编码**

```python
req += bytes([len(host)]) + host + b"\x00\x00"              # ❌ 连到 host:0
req += bytes([len(host)]) + host + port.to_bytes(2, "big")  # ✅
```

### 环境代理隔离

内核实例必须**清掉代理环境变量**，否则出站会走本机代理，检测结果失真：

```python
env = {k: v for k, v in os.environ.items() if "proxy" not in k.lower()}
subprocess.Popen(cmd, env=env, creationflags=subprocess.CREATE_NO_WINDOW)
```

同理，**拉取订阅**也不能依赖环境变量 `https_proxy` —— 它常被工具设成白名单代理
（如 WorkBuddy 的 `127.0.0.1:8426`），只放行部分域名。
策略：显式代理 → 环境代理 → 直连，三级尝试。

### 已知泄漏

`_speed_once` / `_rtt_probe` 起内核进程后，若端口握手失败，
`finally` 里的 `proc.terminate()` 有时抓不住进程，**会残留 sing-box 子进程**
（每个约 60MB 且持有本地 SOCKS 端口）。`stop_v2.bat` 会一并清理，
但根治需要给 terminate 加超时强杀兜底。

---

## 自举依赖（务必理解）

项目内的 `bin/sing-box.exe` / `bin/xray.exe` 是**通用引擎**：
它们能配置并连接任意节点，但**自身不含任何节点**（没有默认配置、
不读系统代理、不连任何服务器）。所以存在前置依赖：

```
① 拉取订阅  →  需要一个【已能上网的外部代理】（用户侧的 v2rayN/Clash）
② 检测节点  →  项目内核按订阅里的地址/uuid/密码起实例，逐个验证
```

**没有 ① 就必然 0 个节点。** 这是设计使然而非 bug。

### 为什么不用项目内核去做 ①

 tempting 的想法是「让内核自己拉订阅」，但那要求内核有一个可用出口 ——
而出口正是待验证的对象，逻辑上循环。**必须用外部已建立的代理。**

### 两条独立路径

| 路径 | 需要外部代理 |
|---|---|
| `--url`（订阅检测） | ✅ |
| `--file` / `--paste`（节点批量） | ❌ |

### 诊断实现

`_preflight()`（Web）与 `_probe_proxy()`（CLI）在检测前探活代理：

```python
opener = urllib.request.build_opener(
    urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
with opener.open("https://www.gstatic.com/generate_204", timeout=8) as r:
    ...   # 通则继续，不通则打印前置依赖说明并 return False
```

用 `generate_204` 而不是订阅源本身 —— 它足够轻量，且不依赖具体站点。

---

## 测试

### 内核配置校验（比启动后再看日志快得多）

```python
import json, subprocess, tempfile, os
def check(cfg, exe="bin/sing-box.exe"):
    p = os.path.join(tempfile.gettempdir(), "c.json")
    open(p, "w", encoding="utf-8").write(json.dumps(cfg))
    return subprocess.run([exe, "check", "-c", p],
                          capture_output=True).returncode == 0
```

`check` 是纯解析，不占网络 —— 改配置后**务必先跑它**，能省掉一轮部署-启动-抓日志。

### 端到端冒烟

```python
import eventlet; eventlet.monkey_patch()   # Web UI 相关测试必须加
import sys; sys.path.insert(0, "src")
from v2ray_checker_1_0_0.web_funnel import run_funnel_green
from v2ray_checker_1_0_0.kernel_probe import find_kernel
```

建议用「一真一假」组合验证分层：
一个能连的节点 + 一个端口不通的 + 一个 uuid 非法的，
期望分别落在 L4 / L2 / L1。

---

## Windows 注意事项

### `.bat` 文件必须 GBK + CRLF

在 Git Bash 里用 `cat > x.bat <<EOF` 会存成 LF-only + UTF-8，
Windows 批处理两个都不认（乱码 + 逐行解析失效）。
**正确写法**：

```python
with open("x.bat", "wb") as f:
    f.write(content.replace("\n", "\r\n").encode("gbk"))
```

另外 GBK 编不了 emoji（🚀 🎯 ✅），脚本里别用。

### 子进程要隐藏窗口

```python
subprocess.Popen(cmd, creationflags=subprocess.CREATE_NO_WINDOW)
```

否则每起一个内核实例都弹黑框。

### 端口排除范围

本机若把动态端口范围改小（如 `1024–15000`），
低端口段会被 WinNAT/Hyper-V 预留，导致 `WSAEACCES`。
排查：`netsh interface ipv4 show excludedportrange protocol=tcp`。