# 变更历史

本文件记录 notable changes。格式参考 [Keep a Changelog](https://keepachangelog.com/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

---

## [2.1.1] — 2026-10-08

### 修复

- **启动脚本 UX**：双击 `start_v2.bat` 后不再「干等没反馈」

  实测服务本身只需 3.3 秒启动，但旧脚本打印完「正在启动，请稍候...」就阻塞，
  既不显示进度也不打开浏览器，用户会以为程序卡死。这个体验问题比任何技术问题
  都更让人困惑。

  | 问题 | 修复 |
  |---|---|
  | 不开浏览器、不知去哪访问 | 就绪后自动打开 `http://localhost:5000` |
  | 打印「正在启动」后无反馈 | 轮询端口探活，**打印实际耗时秒数** |
  | 端口被占用就直接失败 | 启动前自动清理占用 5000 端口的残留进程 |
  | 关掉窗口服务就停 | 创建真正的独立进程，父进程退出不受影响 |
  | 出错只能看到空白 | stdout/stderr 写入 `logs/server.log` |

### 新增

- `launcher.py` —— 以独立进程启动服务

  使用 Python 的 `subprocess` 分离进程标志（`DETACHED_PROCESS`、
  `CREATE_NEW_PROCESS_GROUP`、`CREATE_NO_WINDOW`），
  父进程退出后服务继续存活。

  不依赖 `wmic` 或 PowerShell —— 这两者在部分环境会被安全策略禁用
  （实测本机 `wmic` 被拦）。

- `wait_port.py` —— 端口探活工具（~10ms）

  批处理里没有可靠的「判断 TCP 端口是否已监听」的内建命令。常见替代都有问题：
  `netstat` 看不出服务是否能接受请求；PowerShell 的连接检测每次要 1~2 秒，
  轮询十几秒明显变慢；`ping` 测的是 ICMP，与端口无关。

  本脚本 socket 连接成功即 `exit 0`，纯 Python 无外部依赖。

### 版本号

`pyproject.toml` 与页面标题统一为 `2.1.1`（此前 `pyproject.toml` 仍是 `0.1.0`，
与实际版本不符）。

---

## [2.0.0] — 2026-10-08

### 破坏性变更

- **检测判据从「端口连通」改为「真实内核端到端验证」**

  1.0 只做 `socket.create_connection()` + `ssl.wrap_socket()`，
  从未真正跑过代理 —— 只能证明「端口开着」。
  实测 1.0 判定"有效"的 33 个节点中，真实可用的只有 1 个（**假阳性率 97%**）。

  2.0 启动 sing-box / xray 内核建隧道，拿 `www.gstatic.com/generate_204`
  的真实 HTTP 204 才算通过。

- `/api/results` 返回值从文件数组改为结构化面板数据。
  文件列表移至新接口 `GET /api/result_files`。
  前端已做双契约兼容，1.0 后端仍可工作。

- 配置传递改用 `[vars]` 而非 `wrangler secret`（后者在部分账号上会损坏 Worker，
  且与本项目无关，属历史遗留）。

### 新增

- **五级漏斗**：解析 → TCP → TLS → ★内核端到端 → 隧道测速。
  前三级廉价，能砍掉绝大多数节点；120 节点全流程约 20 秒。

- **评分排序**，权重设计刻意反直觉：
  稳定性 45% > 速度 30% > 延迟 20%。稳定性由「延迟 + 抖动」共同决定，
  抖动按 `min(30, jitter×0.5)` 单独扣分。

- **多协议支持**：VLESS / VMess / Trojan / Shadowsocks / Hysteria2 / TUIC，
  传输覆盖 tcp / ws / grpc / h2 / xhttp。

- **CLI 入口**：`python -m v2ray_checker_1_0_0.cli`，支持 `--url` / `--file` / `--paste`。

- **Web UI 增强**：
  - KPI 卡（解析/TCP/TLS/真实可用/可用率/内核版本）
  - TOP 排行表（含抖动列）
  - 失败原因横向条形图
  - 测速开关、测速节点数、订阅拉取代理三个新选项

- **测速双路径**：测速站可达时测真实带宽；全不可达时降级为 RTT 抖动探测
  （连发 6 次 204 算标准差）。

- **可操作的错误提示**：把`WinError 10054` / `RemoteDisconnected` 等
  底层异常翻译成「请确认 Clash 已启动」这类可照做的建议。

### 修复

- **节点标签去重**：公共订阅的 remark 常直接是 telegram 链接，
  既冗长又无区分度 —— 此类回退显示为「协议 @ 地址:端口」。

- **进度计数溢出**：L1 阶段多调了一次 `tick()`，导致进度显示 `48/33`。

- **订阅拉取的代理处理**：三级策略（显式代理 → 环境代理 → 直连），
  不再依赖环境变量（白名单代理会导致 GitHub Raw 报 502）。

- **`addLog` 契约对齐**：后端补 `timestamp` 字段，修复日志区域空白。

- **前端错误处理兼容两种后端**：`data.error` vs `data.success/message`。

- **`.bat` 编码**：转为 GBK + CRLF（此前是 UTF-8 + LF，Windows 下乱码且无法执行）。

### 已知限制

- **存在自举依赖（前置条件）**

  项目自带内核（sing-box / xray）但**不含节点** —— 它是通用引擎，
  必须外部喂入节点配置才能工作。因此：

  ```
  ① 拉取订阅 → 需要一个【已能上网的外部代理】
  ② 检测节点 → 把 ①拿到的节点喂给内核
  ```

  若 ① 失败，② 必然 0 个节点。**但只有「拉取订阅」这一步需要代理**——
  用 `--file` / `--paste` 直接粘贴节点 URI 时，全程无需任何代理。

  2.0 会检测前自动探活代理，不通时明确提示这是前置依赖问题并给出
  三种解决办法（启动外部代理 / 改用 file 模式 / 直连）。

- 内核二进制需手动下载（仓库不含，约 50MB）
- hysteria2 / tuic 端到端未实测（仅验证配置生成）
- macOS / Linux 未验证（`creationflags` 是 Windows 专有）
- 测速站不可达时只能给出抖动，不能给出带宽
- 内核进程在握手失败时可能残留（`stop_v2.bat` 可清理）

---

## [1.0.0] — 2026-08-22

### 新增

- 初始版本：订阅链接 / 节点批量双模式检测
- Flask + Flask-SocketIO Web UI，实时推送进度与日志
- TCP 连通性 + TLS 握手两级检测
- Base64 / JSON / VMess / VLESS / Trojan / SS 解析
- `start.bat` / `stop.bat` 一键启停

---

## 对照实验数据

为验证 2.0 的必要性，用 1.0 判定的 33 个"有效"节点跑 2.0：

| 层级 | 数量 | 含义 |
|---|---|---|
| 1.0 判定"有效" | 33 | TCP + TLS 能通 |
| **2.0 真实可用** | **1** | 通过内核端到端验证 |
| L4 隧道握手被拒 | 11 | 节点已失效或参数错 |
| L4 隧道通了但目标无响应 | 6 | 出口被墙 |
| L2 TCP 不通 | 13 | 本轮抖动 |

**假阳性率 = (33 - 1) / 33 ≈ 97%**

复现方式：拿 `result/valid_nodes_<时间戳>.txt` 跑 2.0，对比漏斗统计。

---

[2.1.1]: https://github.com/taylormao/v2ray-checker/releases/tag/v2.1.1
[2.0.0]: https://github.com/taylormao/v2ray-checker/releases/tag/v2.0.0
[1.0.0]: https://github.com/taylormao/v2ray-checker/releases/tag/v1.0.0