# v2rayN 订阅节点检测工具

基于 Python 的 v2rayN 订阅节点有效性检测工具，内置 Web UI，支持 **订阅链接检测** 与 **节点批量检测** 双模式，实时展示检测进度与结果。

## 功能特性

- **双模式检测**：订阅链接抓取检测 / 节点 URI 批量检测
- **并发检测**：默认 30 线程并发，超时与并发数可在界面调整
- **实时进度**：WebSocket 推送检测进度、日志与统计
- **结果自动保存**：有效节点与检测日志写入 `result/` 目录，支持界面下载
- **多格式解析**：支持 Base64、JSON、VMess、VLESS、Trojan、SS、Socks 节点格式
- **一键启停**：Windows 下提供 `start.bat` / `stop.bat` 脚本

## 环境要求

- Windows 10 / 11
- [uv](https://docs.astral.sh/uv/)（推荐，脚本基于 uv 管理虚拟环境与依赖）
- Python 3.12（项目通过 `.python-version` 锁定）

## 快速开始

```bash
# 1. 启动（自动同步依赖并启动 Web 服务）
双击 start.bat
# 或命令行
uv run python app.py

# 2. 浏览器访问
http://localhost:5000

# 3. 停止服务
双击 stop.bat
```

## 使用说明

### 订阅链接检测

在 Web 界面左侧「订阅链接检测」面板粘贴一个或多个订阅 URL（每行一个），点击「开始检测」。工具会抓取订阅内容并解析节点，随后并发检测有效性。

### 节点批量检测

在「节点批量检测」面板直接粘贴节点 URI（每行一个，如 `vless://`、`vmess://`、`trojan://`、`ss://`），点击「开始检测」。无法解析的行会以 ⚠️ 提示并跳过，不影响其他节点。

> 提示：`vmess` 节点备注位于 Base64 JSON 的 `ps` 字段；`vless` 节点可用行尾 `#` 或查询参数中的 remark 字段携带备注。

### 检测设置

「检测设置」面板可调整：

| 参数 | 说明 | 默认值 |
|---|---|---|
| 超时时间 | 单节点 TCP/TLS 检测超时（秒） | 3 |
| 并发数 | 同时检测的线程数 | 30 |

### 结果查看

- 有效节点实时输出到 `result/valid_nodes_<时间戳>.txt`
- 完整检测日志输出到 `result/check_log_<时间戳>.txt`
- Web 界面「检测结果」区域可下载结果文件

## 命令行用法

```bash
uv run python check_subscription.py -u <订阅链接> [-u <更多链接>]
uv run python check_subscription.py -f <订阅链接文件>
uv run python check_subscription.py -n <节点列表文件>
uv run python check_subscription.py -u <订阅链接> -t 5 -w 50 -o result/valid.txt
```

| 参数 | 说明 |
|---|---|
| `-u, --url` | 订阅链接，可多次指定 |
| `-f, --file` | 订阅链接文件（每行一个） |
| `-n, --nodes` | 节点列表文件（每行一个节点 URI，不走订阅抓取） |
| `-t, --timeout` | 节点检测超时秒数，默认 3 |
| `-w, --workers` | 并发线程数，默认 30 |
| `-o, --output` | 有效节点输出文件 |
| `--no-color` | 禁用颜色输出（Web UI 内部调用使用） |

## 目录结构

```
v2ray-checker-1.0.0/
├── app.py                    # Flask Web UI 入口
├── check_subscription.py     # 节点检测核心脚本
├── pyproject.toml            # 项目与依赖配置
├── uv.lock                   # 依赖锁定文件
├── .python-version           # Python 版本锁定
├── start.bat                 # 一键启动脚本
├── stop.bat                  # 一键停止脚本
├── templates/index.html      # Web 界面
├── static/                   # 静态资源
├── result/                   # 检测结果输出（不入库）
├── temp/                     # 节点输入临时文件（不入库）
└── src/                      # 脚手架源码
```

## 常见问题

### 端口 5000 被占用

启动时报 `OSError: [WinError 10048]` 说明端口被占用。先运行 `stop.bat` 释放端口，再启动；`start.bat` 已内置端口预检。

### 中文乱码 / `'3]' is not recognized` 类报错

请使用项目自带的 `start.bat` / `stop.bat`（纯 ASCII + CRLF 编码）。自行编辑 bat 时勿用 UTF-8 中文，避免 cmd 按 GBK 解析产生字节错位。

### 子进程崩溃（UnicodeEncodeError）

检测子进程已强制 UTF-8 管道输出，无需额外配置 `PYTHONIOENCODING`。

## 免责声明

本项目仅供学习与技术研究使用，请遵守所在地法律法规，勿用于非法用途。