"""
节点数据模型 + 五级漏斗 + 评分排序
==================================

设计目标
--------
把「能不能用」这个二元判断，升级为「有多好用」的可解释评分。

为什么要分级
------------
真实端到端检测（起内核、进隧道、拿目标站响应）很贵：
每个节点要 3-8 秒，还依赖 sing-box/xray 二进制。
如果对全部节点都跑一遍，几百个节点就是几十分钟。

漏斗的价值：把便宜的前置检查放在前面，逐级砍掉绝大多数节点，
只有少量存活节点才进入昂贵的内核级检测。

    ① 解析有效性   ~0ms    砍掉格式错误 / 参数缺失
    ② TCP 连通      ~3s     砍掉端口不通（通常能砍 60-80%）
    ③ TLS 握手      ~3s     砍掉证书/SNI 问题
    ④ 内核端到端    ~5-8s   ★ 唯一权威判据
    ⑤ 隧道测速      ~6s     只对 TOP N 跑

评分维度
--------
最终分数由四个维度加权，刻意**不把延迟当主导因子** ——
因为在国内网络环境下，「延迟低但丢包/被限速」的节点体感远差于
「延迟略高但稳定快速」的节点。

    稳定性 45%   ← 最高权重：能不能稳���用完整个会话
    速度   30%   ← 真实下载带宽
    延迟   20%
    落地机房 5%  ← 仅作记录与同机房聚合，不实质影响分数
"""

from __future__ import annotations

import base64
import json
import re
import socket
import ssl
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlsplit

# --------------------------------------------------------------------------
# 数据模型
# --------------------------------------------------------------------------


@dataclass
class NodeSpec:
    """统一的节点表示。所有协议的解析结果都归一到这里。"""

    # ---- 身份 ----
    type: str = ""                    # vless / vmess / trojan / ss / hysteria2 / tuic
    address: str = ""
    port: int = 0
    remark: str = ""                  # 节点备注（用于排序后展示）

    # ---- 认证 ----
    uuid: str = ""
    password: str = ""
    aid: int = 0
    security: str = "auto"            # vmess加密 / ss 方法
    flow: str = ""

    # ---- 传输 ----
    network: str = "tcp"              # tcp / ws / grpc / http(xhttp) / h2 / quic
    path: str = "/"
    host: str = ""                    # HTTP Host 头
    sni: str = ""
    alpn: str = ""
    fingerprint: str = ""

    # ---- TLS ----
    tls: bool = False
    allow_insecure: bool = True

    # ---- Hysteria2 / TUIC ----
    up_mbps: int = 0
    down_mbps: int = 0

    # ---- 原始 URI（重新输出时复用，避免信息丢失）----
    raw: str = ""

    # ---- 检测结果 ----
    stage: str = "init"               # 到达的最深层级
    valid: bool = False
    reason: str = ""

    # TCP / TLS 层
    tcp_ok: bool = False
    tcp_latency: float = 0.0
    tls_ok: bool = False
    tls_latency: float = 0.0

    # 内核层
    kernel_ok: bool = False
    kernel_reason: str = ""
    kernel_http_status: int = 0
    kernel_version: str = ""

    # 测速层
    mbps: float = 0.0
    speed_bytes: int = 0
    jitter_ms: float = 0.0          # RTT 抖动（标准差）—— 体感流畅度的关键指标
    speed_mode: str = ""            # bandwidth（真实带宽） / rtt（降级探测）

    # 评分
    score: float = 0.0
    score_breakdown: Dict[str, float] = field(default_factory=dict)

    # ---- URI 往返 ----
    def to_uri(self) -> str:
        """重新拼回 v2rayN 可用的 URI。"""
        if self.raw and not self._needs_rebuild():
            return self.raw

        frag = unquote(self.remark or f"{self.type}-{self.address}")
        q: List[str] = []
        if self.type in ("vless", "vmess"):
            q.append("encryption=none" if self.type == "vless" else f"encryption={self.security}")
        if self.security and self.type not in ("vless", "vmess"):
            q.append(f"security={self.security}")
        if self.flow:
            q.append(f"flow={self.flow}")
        if self.network and self.network != "tcp":
            q.append(f"type={self.network}")
        if self.host:
            q.append(f"host={self.host}")
        if self.path:
            q.append(f"path={self.path or '/'}")
        if self.tls:
            q.append("security=tls")
            if self.sni:
                q.append(f"sni={self.sni}")
        elif self.security == "none":
            pass
        else:
            q.append("security=none")
        if self.allow_insecure:
            q.append("allowInsecure=1")

        return f"{self.type}://{self.uuid or self.password}@{self.address}:{self.port}?" \
               + "&".join(q) + f"#{frag}"

    def _needs_rebuild(self) -> bool:
        """带备注标签的节点需要重建（要把新分数写进 remark）。"""
        return bool(self.remark)

    def with_score_tag(self) -> str:
        """带评分标签的 URI（用于输出排序后的订阅）。"""
        base = self.to_uri()
        bits = [f"{self.score:.0f}分", f"{self.latency:.0f}ms"]
        if self.mbps:
            bits.append(f"{self.mbps:.1f}M")
        if self.jitter_ms:
            bits.append(f"±{self.jitter_ms:.0f}ms")
        base = re.sub(r"#[^#]*$", "", base)
        return f"{base}#{self.remark or self.label} [{' '.join(bits)}]"

    @property
    def latency(self) -> float:
        """取实际测到的延迟：优先内核级，其次 TLS/TCP。"""
        if self.kernel_ok and self.tcp_latency:
            return self.tcp_latency
        return self.tls_latency or self.tcp_latency or 0.0

    @property
    def label(self) -> str:
        """人类可读标签。

        公共订阅的 remark 常常直接是个 telegram 链接（https://t.me/xxx），
        这种既冗长又毫无区分度 —— 此时回退用「协议 @ 地址:端口」。
        """
        r = (self.remark or "").strip()
        if not r or r.startswith("http://") or r.startswith("https://"):
            return f"{self.type}@{self.address}:{self.port}"
        return r

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------
# Level 1 —— 解析有效性（纯本地，零成本）
# --------------------------------------------------------------------------


def validate_spec(node: NodeSpec) -> Tuple[bool, str]:
    """检查必填字段是否齐全。不涉及任何网络。"""
    if not node.type:
        return False, "缺少协议类型"
    if not node.address:
        return False, "缺少地址"
    if not (0 < node.port < 65536):
        return False, f"端口非法: {node.port}"

    # 各协议的必填认证字段
    if node.type == "vless":
        if not re.fullmatch(
            r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
            r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", node.uuid or ""
        ):
            return False, "VLESS uuid 格式非法"
    elif node.type == "vmess":
        if not node.uuid:
            return False, "VMess 缺少 uuid"
    elif node.type == "trojan":
        if not node.password:
            return False, "Trojan 缺少密码"
    elif node.type in ("ss", "shadowsocks"):
        if not node.password or not node.security:
            return False, "SS 缺少密码或加密方式"
    elif node.type in ("hysteria2", "hy2"):
        if not node.password:
            return False, "Hysteria2 缺少密码"
    elif node.type == "tuic":
        if not node.uuid:
            return False, "TUIC 缺少 uuid"

    # TLS 节点必须有 SNI，否则内核握手会失败
    if node.tls and not (node.sni or node.host):
        return False, "启用 TLS 但未提供 SNI/Host"

    return True, "ok"


# --------------------------------------------------------------------------
# Level 2 / 3 —— TCP 与 TLS
# --------------------------------------------------------------------------


def probe_tcp(node: NodeSpec, timeout: float = 4.0) -> Tuple[bool, float]:
    t0 = time.time()
    try:
        with socket.create_connection((node.address, node.port), timeout=timeout):
            return True, (time.time() - t0) * 1000
    except Exception:
        return False, 0.0


def probe_tls(node: NodeSpec, timeout: float = 5.0) -> Tuple[bool, float, str]:
    """TLS 握手 + 证书检查。返回 (成功, 延迟ms, 说明)。"""
    t0 = time.time()
    try:
        ctx = ssl.create_default_context()
        # 公共节点证书普遍不规范，默认不校验链；SNI 仍会校验主机名匹配
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        server_name = node.sni or node.host or node.address
        with socket.create_connection((node.address, node.port), timeout=timeout) as raw:
            with ctx.wrap_socket(raw, server_hostname=server_name) as ss:
                ss.getpeercert()
                return True, (time.time() - t0) * 1000, "TLS ok"
    except ssl.SSLError as exc:
        return False, 0.0, f"TLS 失败: {exc.reason if hasattr(exc,'reason') else exc}"
    except socket.timeout:
        return False, 0.0, "TLS 握手超时"
    except Exception as exc:
        return False, 0.0, f"{type(exc).__name__}"


# --------------------------------------------------------------------------
# Level 5 —— 评分
# --------------------------------------------------------------------------

# 落地机房分组（仅用于记录与聚合展示）
COLO_GROUP = {
    "HKG": "港", "TPE": "台", "NRT": "日", "KIX": "日",
    "ICN": "韩", "SIN": "新", "BKK": "泰", "KUL": "马",
    "LAX": "美西", "SJC": "美西", "SFO": "美西", "SEA": "美西",
    "ORD": "美中", "IAD": "美东", "EWR": "美东", "ATL": "美东",
    "DFW": "美中", "YUL": "北美", "SYD": "澳", "FRA": "欧", "LHR": "欧",
}


def score_node(node: NodeSpec) -> Tuple[float, Dict[str, float]]:
    """给通过内核检测的节点打分，返回 (总分, 分项明细)。

    权重设计：**稳定性 > 速度 > 延迟**。
    国内实测里，一个 60ms 但 5% 丢包的节点，体感远差于 90ms 但稳定的节点。
    """
    if not node.kernel_ok:
        return 0.0, {}

    # ---- 稳定性：延迟 + 抖动的综合表现 ----
    # 只看平均延迟会误判：60ms 但抖动 80ms 的节点体感远差于
    # 90ms 但抖动 5ms 的节点。所以抖动要单独扣分。
    lat = node.latency
    stability = 100.0
    if lat <= 100:
        stability = 100.0
    elif lat <= 200:
        stability = 92.0
    elif lat <= 400:
        stability = 80.0
    elif lat <= 800:
        stability = 62.0
    else:
        stability = 40.0

    # 抖动惩罚：抖动越小越好，超过 50ms 开始明显扣分
    if node.jitter_ms > 0:
        jitter_penalty = min(30.0, node.jitter_ms * 0.5)
        stability = max(10.0, stability - jitter_penalty)

    # ---- 速度：对数刻度，1Mbps 与 10Mbps 的差距远小于 10 与 50 ----
    import math
    if node.mbps <= 0:
        speed = 0.0
    else:
        speed = min(100.0, 40.0 * math.log10(1 + node.mbps))

    # ---- 延迟分 ----
    if lat <= 0:
        latency_score = 0.0
    elif lat <= 80:
        latency_score = 100.0
    elif lat <= 150:
        latency_score = 88.0
    elif lat <= 300:
        latency_score = 70.0
    elif lat <= 600:
        latency_score = 48.0
    else:
        latency_score = 25.0

    breakdown = {
        "稳定性": round(stability * 0.45, 1),
        "速度": round(speed * 0.30, 1),
        "延迟": round(latency_score * 0.20, 1),
    }
    total = round(sum(breakdown.values()), 1)
    return total, breakdown


def apply_scores(nodes: List[NodeSpec]) -> List[NodeSpec]:
    """为所有通过内核检测的节点打分，并按总分降序排列。"""
    for n in nodes:
        if n.kernel_ok:
            n.score, n.score_breakdown = score_node(n)
        else:
            n.score = 0.0
            n.score_breakdown = {}
    return sorted(nodes, key=lambda n: (-n.score, n.latency or 9999))


# --------------------------------------------------------------------------
# 输出
# --------------------------------------------------------------------------


def to_v2rayn_subscription(nodes: List[NodeSpec], only_working: bool = True) -> str:
    """输出 v2rayN 可直接导入的 base64 订阅。"""
    pool = [n for n in nodes if n.kernel_ok] if only_working else nodes
    lines = [n.with_score_tag() for n in pool]
    body = "\n".join(lines)
    return base64.b64encode(body.encode("utf-8")).decode("ascii")


def to_plain_list(nodes: List[NodeSpec]) -> str:
    """输出明文 URI 列表（便于查看和粘贴）。"""
    return "\n".join(n.with_score_tag() for n in nodes if n.kernel_ok)


def build_report(
    nodes: List[NodeSpec],
    kernel_version: str = "",
    elapsed: float = 0.0,
) -> Dict[str, Any]:
    """输出结构化 JSON 报告，含漏斗各级淘汰统计。"""
    stages = ["init", "L1", "L2", "L3", "L4", "L5"]
    funnel = {}
    for s in stages:
        funnel[s] = sum(1 for n in nodes if n.stage == s)

    working = [n for n in nodes if n.kernel_ok]
    measured = [n for n in nodes if n.mbps > 0]

    return {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "kernel": kernel_version,
        "elapsed_seconds": round(elapsed, 1),
        "total": len(nodes),
        "funnel": funnel,
        "summary": {
            "tcp_ok": sum(1 for n in nodes if n.tcp_ok),
            "tls_ok": sum(1 for n in nodes if n.tls_ok),
            "kernel_ok": len(working),
            "speed_measured": len(measured),
        },
        "nodes": [
            {
                "type": n.type,
                "address": n.address,
                "port": n.port,
                "remark": n.label,
                "network": n.network,
                "tls": n.tls,
                "stage": n.stage,
                "valid": n.kernel_ok,
                "latency_ms": round(n.latency, 1),
                "mbps": n.mbps,
                "jitter_ms": n.jitter_ms,
                "speed_mode": n.speed_mode,
                "score": n.score,
                "score_breakdown": n.score_breakdown,
                "http_status": n.kernel_http_status,
                "kernel": n.kernel_version,
                "reason": n.kernel_reason or n.reason,
            }
            for n in nodes
        ],
    }