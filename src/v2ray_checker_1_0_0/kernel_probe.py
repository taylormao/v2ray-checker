"""
真实内核端到端检测（Level 4/5）
================================

为什么必须有这一层
------------------
原有检测只做 `socket.create_connection()` + `ssl.wrap_socket()`，
这只能证明「端口开着」和「有TLS 服务」，不能证明这是个能用的代理。

假阳性场景（本模块就是为了消灭它们）：
  - 端口开放但根本不是代理服务          → 隧道握手直接失败
  - VLESS uuid / flow /加密方式填错→ 隧道握手被服务端拒绝
  - SNI / host / path 任一不匹配       → 路由不到后端
  - 出口被墙（最致命）                 → 本机能连上，但目标站打不开

因此判据必须是：**通过节点自己的隧道，拿到目标站返回的真实 HTTP 响应**。

设计要点
--------
1. 每个节点起一个独立内核实例，用本地 SOCKS 端口做端口分配，避免冲突。
2. 内核启动 → 等端口就绪 → 发真实 HTTP 请求 → 判定→ 优雅关闭。
3. 内核二进制自动探测（sing-box 优先，xray 兜底），都找不到则明确报错而不是
   静默退回TCP 检测（那会让用户误以为检测通过了）。
4. 不污染系统代理：内核的本地 SOCKS 监听在127.0.0.1 的临时端口，
   走`direct` 出站，不改任何系统设置。

授权：仅用于自查自用的节点可用性，不用于对外服务。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

# --------------------------------------------------------------------------
# 内核探测
# --------------------------------------------------------------------------

# 本文件位于 <project>/src/v2ray_checker_1_0_0/kernel_probe.py
# → 上溯三级才是项目根，内核放在 <project>/bin/
BIN_DIR = Path(__file__).resolve().parents[2] / "bin"


class KernelNotFound(RuntimeError):
    """找不到可用的代理内核。"""


def _probe_version(exe: Path) -> Optional[str]:
    """返回内核版本号；不可用则返回 None。"""
    if not exe.exists():
        return None
    try:
        out = subprocess.run(
            [str(exe), "version"],
            capture_output=True, text=True, timeout=15,
        )
        line = (out.stdout or out.stderr or "").strip().splitlines()
        return line[0] if line else "unknown"
    except Exception:
        return None


def find_kernel(prefer: str = "auto") -> Dict[str, Any]:
    """探测可用的内核。

    prefer: auto | sing-box | xray
    返回 {"kind":..., "path":..., "version":...}
    """
    cands = {
        "sing-box": BIN_DIR / "sing-box.exe",
        "xray": BIN_DIR / "xray.exe",
    }
    order = ["sing-box", "xray"] if prefer == "auto" else [prefer]

    found: Dict[str, Any] = {}
    for kind in order:
        ver = _probe_version(cands[kind])
        if ver:
            found[kind] = {"kind": kind, "path": str(cands[kind]), "version": ver}

    if prefer != "auto" and prefer not in found:
        raise KernelNotFound(
            f"指定的 {prefer} 不可用。已探测: "
            + (", ".join(f"{k}={v['version']}" for k, v in found.items()) or "无")
        )
    if not found:
        raise KernelNotFound(
            f"未找到代理内核。请把sing-box.exe 或 xray.exe 放到 {BIN_DIR}"
        )
    return found[prefer] if prefer != "auto" else found["sing-box"]


# --------------------------------------------------------------------------
# 端口分配
# --------------------------------------------------------------------------


def _free_port() -> int:
    """向系统借一个空闲端口（bind 后立刻关闭，由内核随后接管）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_port(port: int, proc: subprocess.Popen, deadline: float) -> bool:
    """等待内核的本地 SOCKS 端口就绪。

    ⚠️ 在 eventlet 环境里 sleep 与 socket 已被patch 成非阻塞，
    所以本函数是「协作式等待」—— 它不会独占线程，可与其他绿线程并发。
    """
    while time.time() < deadline:
        if proc.poll() is not None:
            return False# 内核已退出 = 配置有问题
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.4):
                return True
        except OSError:
            eventlet.sleep(0.1) if _IN_EVENTLET else time.sleep(0.1)
    return False


# 检测是否运行在 eventlet 环境下
try:
    import eventlet  # noqa: F401
    _IN_EVENTLET = True
except ImportError:
    _IN_EVENTLET = False


def _sleep(sec: float) -> None:
    """兼容 sleep：eventlet 下用eventlet.sleep 才能让出CPU。"""
    if _IN_EVENTLET:
        eventlet.sleep(sec)
    else:
        time.sleep(sec)


# --------------------------------------------------------------------------
# 配置生成
# --------------------------------------------------------------------------


def _ws_opts(node: "NodeSpec") -> Dict[str, Any]:
    """从节点抽取传输层参数，归一为 sing-box 的 `transport` 字段。

    ⚠️ 两个版本差异（本项目用sing-box 1.14 验证过）：
      · 1.11+ 用统一的 `transport: {type: ...}`，1.13 起旧的 `ws:`/`grpc:`/`http:`
        平级字段会报 `json: unknown field "ws"` 直接启动失败。
      · 故统一走transport，并保留旧版回退分支以兼容老内核。

    ⚠️ 绝不能返回 type字段 —— 调用方用 ** 展开它，会覆盖协议类型。
    """
    # ---- 新版（1.11+）：统一 transport ----
    if node.network == "ws":
        t: Dict[str, Any] = {"type": "ws", "path": node.path or "/"}
        if node.host:
            t["headers"] = {"Host": node.host}
        if node.alpn:
            t["max_early_data"] = 0
        return {"transport": t}

    if node.network == "grpc":
        return {"transport": {"type": "grpc", "service_name": node.path or ""}}

    if node.network == "http":
        # XHTTP / SplitHTTP
        parts = [p for p in (node.path or "/").split("?") if p]
        t = {"type": "http", "path": parts[0] if parts else "/"}
        if len(parts) > 1:
            t["method"] = parts[1]
        if node.host:
            t["host"] = [node.host]
        return {"transport": t}

    if node.network in ("h2", "http2"):
        t = {"type": "http", "path": "/"}
        if node.host:
            t["host"] = [node.host]
        return {"transport": t}

    if node.network == "quic":
        return {"transport": {"type": "quic"}}

    # ⚠️ 裸 TCP：【不能】写 transport: {type:"tcp"} ——
    #    sing-box 1.14 报 `unknown transport type: tcp`。
    #    正确做法是整个 transport 字段省略，走默认 TCP。
    return {}


def _ws_opts_legacy(node: "NodeSpec") -> Dict[str, Any]:
    """旧版 sing-box（<1.11）的平级传输字段，供内核版本过老时回退。"""
    out: Dict[str, Any] = {"type": node.network or "tcp"}
    if node.network == "ws":
        ws: Dict[str, Any] = {"path": node.path or "/"}
        if node.host:
            ws["headers"] = {"Host": node.host}
        out["ws"] = ws
    elif node.network == "grpc":
        out["grpc"] = {"service_name": node.path or ""}
    return out


def build_singbox_config(node: "NodeSpec", socks_port: int) -> Dict[str, Any]:
    """生成 sing-box 配置（单节点 → local SOCKS 出站）。"""
    tls: Dict[str, Any] = {"enabled": bool(node.tls)}
    if node.tls:
        tls["server_name"] = node.sni or node.host or node.address
        tls["insecure"] = bool(node.allow_insecure)
        if node.alpn:
            tls["alpn"] = [a for a in node.alpn.split(",") if a]

    # ⚠️ QUIC 类协议（hysteria2 / tuic）由协议自身决定传输，
    #    绝不能加 transport 字段 —— 实测报`unknown field "transport"`。
    #    shadowsocks 同理：它是 sing-box 自己实现的加密层，
    #    不走 vless/vmess 那套 transport 抽象（实测报 unknown field "transport"）。
    NO_TRANSPORT = ("hysteria2", "hy2", "tuic", "wireguard", "ss", "shadowsocks")
    if node.type in NO_TRANSPORT:
        return {}

    outbound: Dict[str, Any] = {
        "type": node.type,
        "tag": "proxy",
        "server": node.address,
        "server_port": node.port,
        **_ws_opts(node),
    }
    if node.tls:
        outbound["tls"] = tls

    if node.type == "vless":
        outbound["uuid"] = node.uuid or ""
        if node.flow:
            outbound["flow"] = node.flow
        # sing-box 1.11+ 用 packet_encoding；老版本用 network
        outbound["packet_encoding"] = "xudp"
    elif node.type == "vmess":
        outbound["uuid"] = node.uuid or ""
        outbound["alter_id"] = node.aid or 0
        outbound["security"] = node.security or "auto"
    elif node.type == "trojan":
        outbound["password"] = node.password or node.uuid or ""
    elif node.type in ("shadowsocks", "ss"):
        # ⚠️ sing-box 的协议名是 `shadowsocks`，写 `ss` 报 unknown outbound type
        outbound["type"] = "shadowsocks"
        outbound["password"] = node.password or ""
        outbound["method"] = node.security or "aes-256-gcm"
    elif node.type in ("hysteria2", "hy2"):
        outbound["type"] = "hysteria2"
        outbound["password"] = node.password or ""
        outbound["up_mbps"] = node.up_mbps or 50
        outbound["down_mbps"] = node.down_mbps or 200
    elif node.type == "tuic":
        outbound["type"] = "tuic"
        outbound["uuid"] = node.uuid or ""
        outbound["password"] = node.password or ""

    if node.type not in ("vless", "vmess"):
        outbound.pop("packet_encoding", None)

    return {
        "log": {"level": "warn"},
        "inbounds": [
            {
                "type": "socks",
                "tag": "in",
                "listen": "127.0.0.1",
                "listen_port": socks_port,
            }
        ],
        "outbounds": [outbound],
        "route": {
            "rules": [
                # sing-box 1.11+ 用 rule_actions 取代旧的 inbound 字段；
                # 1.13 起旧字段直接报错（FATAL ... legacy inbound fields are removed）
                {
                    "inbound": "in",
                    "action": "route",
                    "outbound": "proxy",
                }
            ],
            "final": "proxy",
            "auto_detect_interface": True,
        },
    }


def build_xray_config(node: "NodeSpec", socks_port: int) -> Dict[str, Any]:
    """生成 Xray 配置（单节点 → local SOCKS 出站）。

    Xray 的配置格式与 sing-box 不同，且对 VLESS 的 flow 校验更严。
    """
    stream: Dict[str, Any] = {"network": node.network or "tcp"}
    if node.network == "ws":
        ws: Dict[str, Any] = {"path": node.path or "/"}
        if node.host:
            ws["headers"] = {"Host": node.host}
        stream["wsSettings"] = ws
    elif node.network == "grpc":
        stream["grpcSettings"] = {"serviceName": node.path or ""}

    user: Dict[str, Any] = {"id": node.uuid or "", "encryption": "none"}
    if node.flow:
        user["flow"] = node.flow

    settings: Dict[str, Any] = {
        "vnext": [
            {
                "address": node.address,
                "port": node.port,
                "users": [user],
            }
        ]
    }

    sockopt: Dict[str, Any] = {"tcpNoDelay": True}
    stream["sockopt"] = sockopt

    out: Dict[str, Any] = {
        "protocol": "vless",
        "settings": settings,
        "streamSettings": stream,
        "tag": "proxy",
    }
    if node.tls:
        out["streamSettings"]["security"] = "tls"
        out["streamSettings"]["tlsSettings"] = {
            "serverName": node.sni or node.host or node.address,
            "allowInsecure": bool(node.allow_insecure),
        }

    return {
        "log": {"loglevel": "warning"},
        "inbounds": [
            {
                "protocol": "socks",
                "port": socks_port,
                "listen": "127.0.0.1",
                "settings": {"udp": False},
                "tag": "in",
            }
        ],
        "outbounds": [out],
    }


# --------------------------------------------------------------------------
# 真实请求
# --------------------------------------------------------------------------


def _http_via_socks(
    proxy_port: int,
    host: str,
    port: int,
    path: str,
    timeout: float,
    body_limit: Optional[int] = 8192,
) -> Dict[str, Any]:
    """经本地 SOCKS5 端口发起真实 HTTP 请求。

    ⚠️ 这里不能用 http.client 的 CONNECT + 二次 request()：
    CONNECT 之后 socket 所有权已交给隧道，http.client 无法再正确复用它
    （会抛"Bad file descriptor"或静默发不出去）。
    因此手工完成 SOCKS5 握手，然后直接用 socket 发原始 HTTP 报文。
    """
    sock: Optional[socket.socket] = None
    try:
        # ---- 1. 连本地 SOCKS 端口
        sock = socket.create_connection(("127.0.0.1", proxy_port), timeout=timeout)
        sock.settimeout(timeout)

        # ---- 2. SOCKS5 握手（无认证）
        sock.sendall(b"\x05\x01\x00")                       # VER=5, 1 method, no auth
        resp = _recv_exact(sock, 2)
        if not resp or resp[0] != 0x05:
            return {"ok": False, "reason": "SOCKS5 握手无响应"}

        # ---- 3. CONNECT 到目标 ----
        host_b = host.encode("idna") if any(ord(c) > 127 for c in host) else host.encode()
        port_b = int(port).to_bytes(2, "big")                # ⚠️ 端口必须真实编码
        target = host_b + b":" + str(port).encode()           # 供日志阅读
        req = b"\x05\x01\x00\x03"                # VER CMD RSV ATYP=DOMAIN
        req += bytes([len(host_b)]) + host_b + port_b
        sock.sendall(req)

        head = _recv_exact(sock, 4)
        if not head or len(head) < 4:
            return {"ok": False, "reason": "SOCKS5 CONNECT 无响应"}
        if head[1] != 0x00:
            return {
                "ok": False,
                "reason": f"SOCKS5 CONNECT 被拒(rep={head[1]})",
            }

        # 解析 BND.ADDR（长度随 ATYP 变化，必须按ATYP 精确读取，
        # 写错会连带把后面的字节当成端口 —— 曾表现为「连到 0.0.0.0:18」）
        atyp = head[3]
        if atyp == 0x01:      # IPv4:4 字节 + 2 端口
            _recv_exact(sock, 4 + 2)
        elif atyp == 0x03:    # 域名:1 长度 + N + 2 端口
            ln = _recv_exact(sock, 1)
            if not ln:
                return {"ok": False, "reason": "SOCKS5 响应截断"}
            _recv_exact(sock, ln[0] + 2)
        elif atyp == 0x04:    # IPv6: 16 字节 + 2 端口
            _recv_exact(sock, 16 + 2)
        else:
            return {"ok": False, "reason": f"未知 ATYP={atyp}"}

        # ---- 4. 隧道已建立，发真实 HTTP 报文
        http_req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            "User-Agent: Mozilla/5.0 (compatible; node-checker/2.0)\r\n"
            "Accept: */*\r\n"
            "Connection: close\r\n\r\n"
        ).encode()
        sock.sendall(http_req)

        # ---- 5. 读响应 ----
        # 探测连通性时读够状态行即可；测速时需要尽量多读body。
        # max_bytes 越大读得越久，但会拖慢整体检测速度。
        max_bytes = 65536 if body_limit is None else body_limit

        raw = b""
        while len(raw) < max_bytes:
            try:
                chunk = sock.recv(16384)
            except socket.timeout:
                break
            if not chunk:
                break                      # 对端关闭 = 完整读完
            raw += chunk
            if body_limit is not None and b"\r\n\r\n" in raw:
                break                      # 连通性探测：拿到头就够了

        if not raw:
            return {"ok": False, "reason": "隧道建立但目标无响应"}

        head_txt = raw.split(b"\r\n\r\n", 1)[0].decode("iso-8859-1", errors="replace")
        first = head_txt.splitlines()[0] if head_txt else ""
        parts = first.split()
        if len(parts) < 2 or not parts[1].isdigit():
            return {"ok": False, "reason": f"响应首行异常: {first[:80]!r}"}

        status = int(parts[1])
        body = raw.split(b"\r\n\r\n", 1)[1] if b"\r\n\r\n" in raw else b""
        return {
            "ok": True,
            "status": status,
            "headers": head_txt,
            "body_len": len(body),
        }

    except socket.timeout:
        return {"ok": False, "reason": f"请求超时(>{timeout:.0f}s)"}
    except Exception as exc:
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    """精确读满 n 字节（超时抛异常，由调用方捕获）。"""
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            break
        buf += chunk
    return buf


# --------------------------------------------------------------------------
# 主入口
# --------------------------------------------------------------------------


@dataclass
class KernelResult:
    """内核级检测结果。"""

    ok: bool = False
    reason: str = ""
    http_status: int = 0
    elapsed_ms: float = 0.0
    kernel: str = ""
    endpoint: str = ""          # 实测落地机房（如有）
    log_tail: str = ""          # 内核日志尾部，便于排查

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "http_status": self.http_status,
            "elapsed_ms": round(self.elapsed_ms, 1),
            "kernel": self.kernel,
            "endpoint": self.endpoint,
        }


DEFAULT_TARGET_HOST = "www.gstatic.com"
DEFAULT_TARGET_PORT = 80
DEFAULT_TARGET_PATH = "/generate_204"


def verify_node(
    node: "NodeSpec",
    kernel: Dict[str, Any],
    *,
    target_host: str = DEFAULT_TARGET_HOST,
    target_port: int = DEFAULT_TARGET_PORT,
    target_path: str = DEFAULT_TARGET_PATH,
    expect_status: int = 204,
    timeout: float = 8.0,
    startup_timeout: float = 6.0,
) -> KernelResult:
    """用真实内核端到端验证节点是否真的能访问目标站。

    这是唯一权威判据 —— 只有拿到目标站的真实响应才算通过。
    """
    kind = kernel["kind"]
    exe = kernel["path"]
    socks_port = _free_port()

    cfg = (
        build_singbox_config(node, socks_port)
        if kind == "sing-box"
        else build_xray_config(node, socks_port)
    )

    tmpdir = Path(tempfile.mkdtemp(prefix="nodecfg_"))
    cfg_path = tmpdir / "config.json"
    log_path = tmpdir / f"{kind}.log"
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")

    if kind == "sing-box":
        cmd = [exe, "run", "-c", str(cfg_path), "-D", str(tmpdir)]
    else:
        cmd = [exe, "run", "-c", str(cfg_path)]

    env = dict(os.environ)
    env.pop("HTTP_PROXY", None)
    env.pop("HTTPS_PROXY", None)
    env.pop("http_proxy", None)
    env.pop("https_proxy", None)

    t0 = time.time()
    proc: Optional[subprocess.Popen] = None
    try:
        lf = open(log_path, "wb")
        proc = subprocess.Popen(
            cmd, stdout=lf, stderr=subprocess.STDOUT,
            env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

        if not _wait_port(socks_port, proc, time.time() + startup_timeout):
            log_tail = _tail(log_path)
            reason = "内核未能建立本地代理端口"
            if proc.poll() is not None:
                reason = f"内核启动即退出(exit={proc.returncode})"
            return KernelResult(
                ok=False, reason=reason, kernel=f"{kind} {kernel['version']}",
                log_tail=log_tail,
            )

        res = _http_via_socks(
            socks_port, target_host, target_port, target_path, timeout
        )
        elapsed = (time.time() - t0) * 1000

        if not res["ok"]:
            return KernelResult(
                ok=False, reason=res["reason"], elapsed_ms=elapsed,
                kernel=f"{kind} {kernel['version']}", log_tail=_tail(log_path),
            )

        status = res["status"]
        if status != expect_status:
            return KernelResult(
                ok=False,
                reason=f"目标返回 HTTP {status}（期望 {expect_status}）",
                http_status=status, elapsed_ms=elapsed,
                kernel=f"{kind} {kernel['version']}", log_tail=_tail(log_path),
            )

        return KernelResult(
            ok=True,
            reason=f"隧道内拿到 HTTP {status}",
            http_status=status,
            elapsed_ms=elapsed,
            kernel=f"{kind} {kernel['version']}",
            log_tail=_tail(log_path),
        )

    except Exception as exc:
        return KernelResult(
            ok=False, reason=f"检测异常: {type(exc).__name__}: {exc}",
            kernel=f"{kind} {kernel.get('version','')}",
        )
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
        shutil.rmtree(tmpdir, ignore_errors=True)


def _tail(path: Path, n: int = 400) -> str:
    """读日志尾部，便于定位握手失败原因。"""
    try:
        if path.exists():
            data = path.read_bytes()[-4000:]
            return data.decode("utf-8", errors="replace")[-n:].strip()
    except Exception:
        pass
    return ""


# 测速目标（按顺序尝试，任一可用即用）
# ⚠️ 结构性约束：公共测速站（speed.cloudflare.com / cachefly / proof.ovh 等）
#    在国内**都需要走代理**，而节点出口未必通它们 —— 实测大量节点对所有
#    测速站均 SOCKS5 CONNECT rep=1，但generate_204 却能过。
#    所以必须把「小响应探测」与「大文件下载」分开：
#      · 测速站全不通时，回退用generate_204 连发估算 RTT/稳定性（不给 Mbps，
#        但能反映体感流畅度），并在结果里标记 measured="rtt" 而非 "bandwidth"。
SPEED_TARGETS = (
    ("cachefly.cachefly.net", 443, "/1mb.test"),
    ("speed.cloudflare.com", 443, "/__down?bytes=8000000"),
    ("proof.ovh.net", 80, "/files/1Mb.dat"),
    ("speedtest.tele2.net", 80, "/1MB.zip"),
)


def speed_test(
    node: "NodeSpec",
    kernel: Dict[str, Any],
    *,
    seconds: float = 6.0,
) -> Dict[str, Any]:
    """Level 5：真实性能测量。

    两条路径：
      A. 大文件下载 → 真实 Mbps（需测速站可达）
      B. 204 连发探测 → RTT +抖动（测速站都不通时的降级方案）

    只对通过 Level 4 的少量节点调用 —— 这一步最慢。
    """
    last_err = ""
    for host, port, path in SPEED_TARGETS:
        res = _speed_once(node, kernel, host, port, path, seconds)
        if res.get("ok"):
            res["target"] = host
            res["measured"] = "bandwidth"
            return res
        last_err = res.get("reason", "unknown")

    # ---- 降级：用 generate_204 连发估算 RTT 稳定性 ----
    rtt = _rtt_probe(node, kernel, count=6)
    if rtt["ok"]:
        rtt["measured"] = "rtt"
        rtt["mbps"] = 0.0
        rtt["target"] = DEFAULT_TARGET_HOST
        rtt["note"] = (
            f"测速站不可达（{last_err}），降级为 RTT 探测："
            f"平均 {rtt['avg_ms']:.0f}ms，抖动 {rtt['jitter_ms']:.0f}ms"
        )
        return rtt

    return {
        "ok": False,
        "reason": f"测速站全不通({last_err})且 RTT 探测也失败({rtt.get('reason','')})",
        "mbps": 0.0,
    }


def _rtt_probe(
    node: "NodeSpec",
    kernel: Dict[str, Any],
    *,
    count: int = 6,
) -> Dict[str, Any]:
    """连发若干次 generate_204，统计 RTT 与抖动。

    抖动（标准差）比平均延迟更能反映体感流畅度 —— 这也是评分里
    「稳定性权重高于速度」的原因所在。
    """
    kind = kernel["kind"]
    socks_port = _free_port()
    cfg = (
        build_singbox_config(node, socks_port)
        if kind == "sing-box"
        else build_xray_config(node, socks_port)
    )
    tmpdir = Path(tempfile.mkdtemp(prefix="nodertt_"))
    cfg_path = tmpdir / "config.json"
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    cmd = (
        [kernel["path"], "run", "-c", str(cfg_path), "-D", str(tmpdir)]
        if kind == "sing-box"
        else [kernel["path"], "run", "-c", str(cfg_path)]
    )
    env = {k: v for k, v in os.environ.items() if "proxy" not in k.lower()}

    proc = None
    rtts: List[float] = []
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if not _wait_port(socks_port, proc, time.time() + 6.0):
            return {"ok": False, "reason": "内核未就绪"}

        for _ in range(count):
            t0 = time.time()
            r = _http_via_socks(
                socks_port, DEFAULT_TARGET_HOST, DEFAULT_TARGET_PORT,
                DEFAULT_TARGET_PATH, timeout=6.0,
            )
            if not r["ok"] or r.get("status") != 204:
                return {"ok": False, "reason": r.get("reason", "非 204 响应")}
            rtts.append((time.time() - t0) * 1000)

        if len(rtts) < 2:
            return {"ok": False, "reason": "样本不足"}

        avg = sum(rtts) / len(rtts)
        var = sum((x - avg) ** 2 for x in rtts) / len(rtts)
        return {
            "ok": True,
            "samples": len(rtts),
            "avg_ms": round(avg, 1),
            "min_ms": round(min(rtts), 1),
            "max_ms": round(max(rtts), 1),
            "jitter_ms": round(var** 0.5, 1),
        }
    except Exception as exc:
        return {"ok": False, "reason": str(exc)}
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
        shutil.rmtree(tmpdir, ignore_errors=True)


def _speed_once(
    node: "NodeSpec",
    kernel: Dict[str, Any],
    target_host: str,
    target_port: int,
    target_path: str,
    seconds: float,
) -> Dict[str, Any]:
    """在单个测速站上跑一次下载测速。"""
    kind = kernel["kind"]
    socks_port = _free_port()
    cfg = (
        build_singbox_config(node, socks_port)
        if kind == "sing-box"
        else build_xray_config(node, socks_port)
    )
    # 测速站多为 HTTPS，必须开 TLS
    if not cfg["outbounds"][0].get("tls"):
        cfg["outbounds"][0]["tls"] = {
            "enabled": True,
            "server_name": node.sni or target_host,
            "insecure": True,
        }

    tmpdir = Path(tempfile.mkdtemp(prefix="nodespeed_"))
    cfg_path = tmpdir / "config.json"
    log_path = tmpdir / f"{kind}.log"
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    if kind == "sing-box":
        cmd = [kernel["path"], "run", "-c", str(cfg_path), "-D", str(tmpdir)]
    else:
        cmd = [kernel["path"], "run", "-c", str(cfg_path)]

    env = dict(os.environ)
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        env.pop(k, None)

    proc = None
    t0 = time.time()
    try:
        lf = open(log_path, "wb")
        proc = subprocess.Popen(
            cmd, stdout=lf, stderr=subprocess.STDOUT, env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if not _wait_port(socks_port, proc, time.time() + 6.0):
            return {"ok": False, "reason": "内核未就绪", "mbps": 0.0}

        total, deadline = 0, time.time() + seconds
        while time.time() < deadline:
            chunk = _http_via_socks(
                socks_port, target_host, target_port, target_path,
                timeout=3.0, body_limit=None,
            )
            if not chunk["ok"] or not chunk.get("body_len"):
                break
            total += chunk["body_len"]

        if total <= 0:
            return {"ok": False, "reason": "下载无数据", "mbps": 0.0}

        dur = max(time.time() - t0, 0.001)
        mbps = (total * 8 / 1e6) / dur
        return {
            "ok": True,
            "bytes": total,
            "seconds": round(dur, 2),
            "mbps": round(mbps, 2),
        }
    except Exception as exc:
        return {"ok": False, "reason": str(exc), "mbps": 0.0}
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
        shutil.rmtree(tmpdir, ignore_errors=True)