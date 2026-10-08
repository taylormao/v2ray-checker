# v2ray-checker 2.0 — 真实内核验证版节点检测

> 端口能通≠ 节点能用。
> 本工具用**真实代理内核**建隧道，拿到目标站的真实响应才算通过 ——
> 消除「端口开着但根本不能用」的假阳性。

[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/)
[![License MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

---

## 这是什么

订阅节点检测工具。输入订阅链接或节点列表，输出**真正能用**的节点，并按综合评分排序。

支持两种入口：**Web UI**（浏览器图形界面）与 **CLI**（命令行）。

---

## 为什么需要 2.0

1.0 版的检测只做两件事：

```python
socket.create_connection((host, port))   # 端口通不通
ssl.wrap_socket(...)                     # 有没有 TLS 服务
```

**它从来没真正跑过代理。** 所以只能证明「端口开着」，而这跟能不能上网几乎无关。

实测交叉验证（拿 1.0 判定"有效"的 33 个节点跑 2.0）：

| | 数量 |
|---|---|
| 1.0 判定"有效" | 33 |
| **2.0 真实可用** | **1** |
| 隧道握手被拒（节点已失效） | 11 |
| 隧道建了但出不去（出口受限） | 6 |
| TCP 不通 | 13 |

**假阳性率 97%。**

2.0 能识别出 1.0 完全看不出的几类问题：

| 场景 | 1.0 判定 | 2.0 判定 |
|---|---|---|
| 端口开放但不是代理服务 | 有效 | ❌ 隧道握手被拒 |
| VLESS uuid / flow 填错 | 有效 | ❌ 握手被拒 |
| SNI / host / path 不匹配 | 有效 | ❌ 握手被拒或路由失败 |
| 能连但**墙内访问不了目标站** | 有效 | ❌ 目标无响应 |
| 服务端返回 301/302 重定向 | 有效 | ❌ `unexpected HTTP response status: 301` |

---

## 核心特性

### 五级漏斗

```
① 解析有效性   ~0ms      必填字段、uuid 格式、TLS 与 SNI 配套
② TCP 连通      ~4s       砍掉端口不通
③ TLS 握手      ~5s       砍掉证书/SNI 问题
④ ★ 内核端到端  ~8s唯一权威判据
⑤ 隧道测速      ~4-6s     只对 TOP N 跑
```

前三级便宜，能砍掉绝大多数节点；只有存活节点才进入昂贵的内核级检测。
120 个节点全流程约 20 秒。

### 第 ④ 级是唯一权威判据

只有「**通过节点自己的隧道，拿到目标站返回的真实 HTTP 响应**」才能证明能用。
判据：`www.gstatic.com/generate_204` 返回 **HTTP 204**。

实现方式：启动 sing-box / xray 内核实例 → 本地 SOCKS 端口 → 手工完成 SOCKS5 握手 →
发真实 HTTP 报文 → 判定 → 关闭。**不自己实现代理协议**，内核负责握手与加解密。

### 评分排序

刻意**不把延迟当主导因子** —— 国内网络环境下，
「延迟低但抖动大」的节点体感远差于「延迟略高但稳定」的节点。

| 维度 | 权重 | 说明 |
|---|---|---|
| **稳定性** | 45% | 由延迟 + **抖动**共同决定。抖动按 `min(30, jitter×0.5)` 单独扣分 |
| **速度** | 30% | 对数刻度，1Mbps→10Mbps 的差距远小于 10→50 |
| **延迟** | 20% | |
| 落地机房 | 5% | 仅记录，不实质影响分数 |

实测验证：某节点 `287ms ±68ms` 时评分从 50 降到 **36** ——
它延迟不高但抖动大，体感会卡。

---

## 环境要求

| 项 | 要求 |
|---|---|
| OS | Windows 10 / 11（macOS/Linux 理论可用，未实测） |
| Python | 3.12+ |
| 包管理 | [uv](https://docs.astral.sh/uv/)（推荐） |
| 内核 | sing-box 或 xray（放在 `bin/`，见下方安装） |

---

## 快速开始

### 1. 安装依赖

```bash
uv sync
```

### 2. 安装内核（二选一）

项目需要真实的代理内核才能验证节点。仓库不含二进制（`.gitignore` 已排除）。

**sing-box（推荐）** —— 协议覆盖最广：

```bash
curl -L -o sb.zip https://github.com/SagerNet/sing-box/releases/download/v1.14.2/sing-box-1.14.2-windows-amd64.zip
unzip sb.zip && mv sing-box-1.14.2-windows-amd64/sing-box.exe bin/
```

**xray** —— 备选：

```bash
curl -L -o xr.zip https://github.com/XTLS/Xray-core/releases/download/v26.3.27/Xray-windows-64.zip
unzip xr.zip && mv Xray.exe bin/xray.exe
```

> 只做连通性验证时，`xray.exe` 附带的 `geoip.dat` / `geosite.dat` 可删（省 30MB）。

### 3. 启动

**Web UI：**

```bash
双击 start_v2.bat       # 浏览器打开 http://localhost:5000
双击 stop_v2.bat        # 停止
```

**CLI：**

```bash
# 从订阅链接
uv run python -m v2ray_checker_1_0_0.cli --url https://example.com/sub

# 从文件
uv run python -m v2ray_checker_1_0_0.cli --file nodes.txt

# 管道输入
cat nodes.txt | uv run python -m v2ray_checker_1_0_0.cli --paste

# 带测速（只测 TOP 10）
uv run python -m v2ray_checker_1_0_0.cli --url <URL> --speed --top 10
```

### 4. ⚠️ 前置依赖（重要，请先读）

本工具**自带内核但不自带节点**，存在一个自举依赖：

```
项目内的 sing-box.exe / xray.exe = 通用引擎（可配任何节点），本身不含节点
                    ↓
① 拉取订阅  →  需要一个【已能上网的外部代理】（v2rayN / Clash 等）
                    ↓
② 检测节点  →  把 ①拿到的节点喂给内核逐个验证
```

**若 ① 失败，② 必然是 0 个节点 —— 这不是工具故障，而是缺前置条件。**

因此有两条独立的使用路径：

| 路径 | 是否需要代理 | 适用场景 |
|---|---|---|
| **节点批量检测**（`--file` / `--paste`） | ❌ **完全不需要** | 你手上已有节点 URI |
| **订阅链接检测**（`--url`） | ✅ 需要一个能上外网的代理 | 订阅源在墙内不可直连 |

换句话说：**只要不是「拉取订阅」这一步，本工具全程无需任何代理。**

#### 配置拉取订阅的代理

界面上的「订阅拉取代理」或 CLI 的 `--proxy`：

| 场景 | 填写 |
|---|---|
| GitHub Raw 等境外源 | `http://127.0.0.1:10808`（v2rayN / Clash 的本地端口） |
| 国内 CDN 源 | `none`（直连更快） |

> ⚠️不要依赖环境变量 `https_proxy` —— 它常被工具设成白名单代理
> （如某些 IDE 的 `127.0.0.1:8426`），只放行部分域名，
> 访问 GitHub Raw 会报 `Tunnel connection failed: 502`。

2.0 会在检测前**自动探活代理**。若代理不通，会明确提示这是前置依赖问题
并给出三种解决办法，而不是让用户对着「0 个可用节点」猜原因。

---

## 输出

| 文件 | 用途 |
|---|---|
| `result/sub_<时间戳>.txt` | **base64 订阅**，v2rayN 可直接导入，备注含评分 |
| `result/valid_<时间戳>.txt` | 明文 URI 列表，便于查看粘贴 |
| `result/report_<时间戳>.json` | 结构化报告：漏斗各级淘汰数、失败原因分布、每节点全指标 |

Web UI 额外提供：KPI 卡、TOP 排行表、失败原因横向条形图。

---

## 支持的协议

| 协议 | 传输支持 |
|---|---|
| **VLESS** | tcp / ws / grpc / h2 / xhttp |
| **VMess** | tcp / ws |
| **Trojan** | tcp / ws / grpc |
| **Shadowsocks** | tcp / ws |
| **Hysteria2** | （配置生成已验证，端到端未实测） |
| **TUIC** | （同上） |

订阅格式：base64 订阅、纯 URI 列表、v2rayN JSON。

---

## 已知限制

| 限制 | 说明 |
|---|---|
| **内核需手动下载** | 仓库不含二进制，见「安装内核」 |
| **带宽常测不出** | 公共测速站在墙内需代理，而节点出口未必通它们。此时降级用 RTT 抖动探测（见下） |
| **QUIC 类未实测** | hysteria2 / tuic 的传输行为与 vless 不同，仅验证了配置生成 |
| **macOS/Linux 未验证** | 代码本身跨平台，但 `creationflags` 是 Windows 专有的 |
| **1.0 版仍在仓库** | `app.py` / `check_subscription.py` 作为回退保留，默认不用 |

### 关于测速降级

公共测速站（`speed.cloudflare.com` / `cachefly` / `proof.ovh`）
在国内**全都需要走代理**，而节点出口未必通它们。实测大量节点对所有测速站
均返回 `SOCKS5 CONNECT rep=1`，但 `generate_204` 却能过。

因此测速做成两条路径：

| 路径 | 条件 | 产出 |
|---|---|---|
| A. 带宽测量 | 某测速站可达 | 真实 Mbps，`speed_mode="bandwidth"` |
| B. **降级：RTT 探测** | 测速站全不通 | 平均/最小/最大延迟 + **抖动标准差**，`speed_mode="rtt"` |

降级不给 Mbps 但给抖动 —— 而抖动恰恰是体感流畅度的关键指标，
比一个假的 0 Mbps 有意义得多。

---

## 项目结构

```
src/v2ray_checker_1_0_0/
├── pipeline.py        NodeSpec 数据模型、五级漏斗、评分排序、输出
├── kernel_probe.py    内核探测、配置生成、SOCKS5 客户端、端到端验证、测速
├── web_funnel.py      eventlet 适配版漏斗（供 Web UI 使用）
├── cli.py             命令行入口 + 多协议 URI 解析
└── testkit.py         测试专用再导出（不部署）

app_v2.py              Web UI 后端（推荐入口）
app.py                 Web UI 后端（1.0 遗留，默认不用）
check_subscription.py  1.0 CLI（遗留，可作对照）
templates/index.html   Web UI 模板（1.0 基础上增量改造）
bin/                   内核二进制（gitignore）
result/                检测输出（gitignore）
```

---

## 开发

```bash
# 类型/语法检查
uv run python -m py_compile src/v2ray_checker_1_0_0/*.py app_v2.py

# 验证内核可用
uv run python -c "
import sys; sys.path.insert(0,'src')
from v2ray_checker_1_0_0.kernel_probe import find_kernel
print(find_kernel('auto'))
"

# 校验生成的 sing-box 配置
sing-box check -c <config.json>
```

### ⚠️ 版本差异：sing-box 1.11~1.14

升级/降级 sing-box 时注意这些字段差异（都用 `check -c` 验证过）：

| 差异 | 表现 |
|---|---|
| 传输层必须用统一 `transport` 字段 | 旧 `ws:`/`grpc:` 平级字段报 `unknown field "ws"` |
| 裸 TCP 不能写 `transport: {type:"tcp"}` | 报 `unknown transport type: tcp`，要整个省略 |
| `hysteria2`/`tuic`/`wireguard`/`ss` 不能加 `transport` | 报 `unknown field "transport"` |
| 协议名是 `shadowsocks` 不是 `ss` | 报 `unknown outbound type: ss` |
| `inbounds` 旧字段已被 `route.rules[].action` 取代 | 1.13+ 直接 FATAL |

---

## 文档

- [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md) —— 架构说明、踩坑记录、实现细节
- [`docs/CHANGELOG.md`](docs/CHANGELOG.md) —— 版本变更历史
- [`OPTIMIZATION.md`](OPTIMIZATION.md) —— 2.0 改造的完整技术说明（含对照实验数据）

---

## 安全与合规

本工具仅用于**自查自用的节点可用性验证**。

- 内核出站不受本机系统代理影响（每个实例独立 SOCKS，用完即关）
- 不修改任何系统网络设置
- 请遵守当地法律法规与服务商条款

## License

MIT — 见 [LICENSE](LICENSE)