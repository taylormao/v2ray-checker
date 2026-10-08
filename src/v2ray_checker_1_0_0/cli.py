"""
节点检测 CLI —— 五级漏斗 + 评分排序
=================================

用法
----
    # 从订阅链接检测
    python -m v2ray_checker_1_0_0.cli --url <订阅URL> [--url <更多>]

    # 从文本文件检测（每行一个 URI）
    python -m v2ray_checker_1_0_0.cli --file nodes.txt

    # 直接粘URI
    python -m v2ray_checker_1_0_0.cli --paste

常用选项
--------
    --no-kernel       只跑到 L3（TCP+TLS），不起内核 —— 快但有假阳性
    --kernel auto|sing-box|xray
    --speed/--no-speed是否对存活节点做隧道测速
    --top N           只对前 N 名测速（默认 10）
    --json报告.json   输出结构化报告
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import List, Optional
from urllib.parse import parse_qs, unquote, urlsplit
from urllib.error import URLError
from urllib.request import (
    ProxyHandler, Request, build_opener,
)

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    __package__ = "v2ray_checker_1_0_0"

from .kernel_probe import (
    KernelNotFound, find_kernel, speed_test, verify_node,
)
from .pipeline import (
    NodeSpec, apply_scores, build_report, probe_tcp, probe_tls,
    to_plain_list, to_v2rayn_subscription, validate_spec,
)


# --------------------------------------------------------------------------
# URI 解析（多协议 → NodeSpec）
# --------------------------------------------------------------------------


def parse_vless(uri: str) -> Optional[NodeSpec]:
    try:
        u = urlsplit(uri)
        q = parse_qs(u.query)
        sni = (q.get("sni") or q.get("peer") or [""])[0]
        host = (q.get("host") or [""])[0]
        security = (q.get("security") or [""])[0]
        return NodeSpec(
            type="vless",
            address=u.hostname or "",
            port=u.port or 0,
            uuid=unquote(u.username or ""),
            network=(q.get("type") or ["tcp"])[0],
            path=unquote((q.get("path") or ["/"])[0]),
            host=host,
            sni=sni,
            alpn=(q.get("alpn") or [""])[0],
            flow=(q.get("flow") or [""])[0],
            fingerprint=(q.get("fp") or [""])[0],
            tls=security in ("tls", "reality", "xtls"),
            allow_insecure=(q.get("allowInsecure") or ["1"])[0] != "0",
            remark=unquote(u.fragment or ""),
            raw=uri,
        )
    except Exception:
        return None


def parse_vmess(uri: str) -> Optional[NodeSpec]:
    """vmess:// 是 base64(JSON)，不是标准 URL。"""
    try:
        body = uri[len("vmess://"):].split("#")[0]
        data = json.loads(base64.b64decode(body + "=" * (-len(body) % 4)))
        host = str(data.get("host", ""))
        return NodeSpec(
            type="vmess",
            address=str(data.get("add", "")),
            port=int(data.get("port", 0) or 0),
            uuid=str(data.get("id", "")),
            aid=int(data.get("aid", 0) or 0),
            security=str(data.get("scy", data.get("security", "auto")) or "auto"),
            network=str(data.get("net", "tcp") or "tcp"),
            path=str(data.get("path", "/") or "/"),
            host=host,
            sni=str(data.get("sni", data.get("tls", "") if isinstance(data.get("tls"), str) else "")),
            tls=str(data.get("tls", "")).lower() in ("tls", "true", "1"),
            allow_insecure=str(data.get("allowInsecure", "1")) != "0",
            remark=str(data.get("ps", "") or ""),
            raw=uri,
        )
    except Exception:
        return None


def parse_trojan(uri: str) -> Optional[NodeSpec]:
    try:
        u = urlsplit(uri)
        q = parse_qs(u.query)
        sni = (q.get("sni") or q.get("peer") or [""])[0]
        return NodeSpec(
            type="trojan",
            address=u.hostname or "",
            port=u.port or 0,
            password=unquote(u.username or ""),
            network=(q.get("type") or ["tcp"])[0],
            path=unquote((q.get("path") or ["/"])[0]),
            host=(q.get("host") or [""])[0],
            sni=sni,
            alpn=(q.get("alpn") or [""])[0],
            tls=True,
            allow_insecure=(q.get("allowInsecure") or ["1"])[0] != "0",
            remark=unquote(u.fragment or ""),
            raw=uri,
        )
    except Exception:
        return None


def parse_ss(uri: str) -> Optional[NodeSpec]:
    """ss:// 有两种形态：SIP002（ss://base64(method:pw)@host:port#tag）"""
    try:
        u = urlsplit(uri)
        if u.username:                       # SIP002
            method_pw = unquote(u.username)
            if ":" in method_pw:
                method, pw = method_pw.split(":", 1)
            else:
                # base64 形式
                pad = "=" * (-len(method_pw) % 4)
                method, pw = base64.b64decode(method_pw + pad).decode().split(":", 1)
            q = parse_qs(u.query)
            return NodeSpec(
                type="ss",
                address=u.hostname or "",
                port=u.port or 0,
                password=pw,
                security=method,
                network=(q.get("type") or ["tcp"])[0],
                path=unquote((q.get("path") or ["/"])[0]),
                host=(q.get("host") or [""])[0],
                remark=unquote(u.fragment or ""),
                raw=uri,
            )
        # 旧式：ss://base64(method:pw@host:port)#tag
        body = uri[len("ss://"):].split("#")[0]
        pad = "=" * (-len(body) % 4)
        decoded = base64.b64decode(body + pad).decode("utf-8", errors="ignore")
        return parse_ss("ss://" + decoded)
    except Exception:
        return None


def parse_uri(uri: str) -> Optional[NodeSpec]:
    """按协议分发。返回 None 表示无法解析。"""
    uri = uri.strip()
    if not uri or uri.startswith("#"):
        return None
    low = uri.lower()
    try:
        if low.startswith("vless://"):
            return parse_vless(uri)
        if low.startswith("vmess://"):
            return parse_vmess(uri)
        if low.startswith("trojan://"):
            return parse_trojan(uri)
        if low.startswith("ss://"):
            return parse_ss(uri)
        if low.startswith("hy2://") or low.startswith("hysteria2://"):
            u = urlsplit(uri)
            return NodeSpec(
                type="hysteria2",
                address=u.hostname or "", port=u.port or 0,
                password=unquote(u.username or ""),
                tls=True, sni=unquote(u.fragment or "") or (u.hostname or ""),
                remark=unquote(u.fragment or ""), raw=uri,
            )
        if low.startswith("tuic://"):
            u = urlsplit(uri)
            q = parse_qs(u.query)
            return NodeSpec(
                type="tuic",
                address=u.hostname or "", port=u.port or 0,
                uuid=unquote(u.username or ""),
                password=(q.get("password") or [""])[0],
                tls=True, sni=u.hostname or "", remark=unquote(u.fragment or ""),
                raw=uri,
            )
    except Exception:
        return None
    return None


# --------------------------------------------------------------------------
# 订阅内容获取与解析
# --------------------------------------------------------------------------


def fetch_subscription(
    url: str,
    timeout: float = 25.0,
    proxy: Optional[str] = None,
) -> str:
    """拉取订阅内容。

    国内网络的现实：订阅源分两类，两类都要支持。

      · GitHub Raw 等境外源 —— 直连必被重置（WinError 10054/10060），
        必须经本地代理。
      · 部分国内 CDN 源 —— 走代理反而更慢或失败。

    ⚠️ 必须用 `proxy` 参数显式指定代理，**不要依赖环境变量**：
    本机环境里`https_proxy` 常被 WorkBuddy 等工具设成白名单代理
    （如 127.0.0.1:8426），它只放行部分域名，访问 GitHub Raw 会报
    `Tunnel connection failed: 502 Bad Gateway` —— 而 Clash 在 10808。

    策略：**显式代理优先 → 环境代理 → 直连**，全失败才抛错。
    """
    req = Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) node-checker/2.0",
        "Accept": "*/*",
    })
    errors: List[str] = []

    # ① 显式指定的代理（最可靠）
    if proxy:
        try:
            ph = ProxyHandler({"http": proxy, "https": proxy})
            with build_opener(ph).open(req, timeout=timeout) as resp:
                data = resp.read().decode("utf-8", errors="ignore")
                if data.strip():
                    return data
            errors.append("显式代理返回空内容")
        except Exception as exc:
            errors.append(f"显式代理失败({type(exc).__name__})")

    # ② 环境代理
    env_proxy = os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY")
    if env_proxy and env_proxy != proxy:
        try:
            ph = ProxyHandler({"http": env_proxy, "https": env_proxy})
            with build_opener(ph).open(req, timeout=timeout) as resp:
                data = resp.read().decode("utf-8", errors="ignore")
                if data.strip():
                    return data
            errors.append("环境代理返回空内容")
        except Exception as exc:
            errors.append(f"环境代理失败({type(exc).__name__})")

    # ③ 直连兜底
    try:
        with build_opener(ProxyHandler({})).open(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="ignore")
    except Exception as exc:
        errors.append(f"直连失败({type(exc).__name__})")

    # 把「代理连不上」翻译成用户能直接照做的建议 —— 原始异常对用户无意义
    hint = ""
    joined = "; ".join(errors)
    if any(k in joined for k in
           ("10061", "拒绝", "无法连接", "RemoteDisconnected",
            "ConnectionRefused", "Tunnel connection failed")):
        hint = ("\n   → 代理端口连不上。若用 Clash，请确认它已启动且混合端口为 "
                f"{proxy or '10808'}；也可把代理填 none 走直连"
                "（国内 CDN 源不需要代理）")
    elif any(k in joined for k in ("10054", "重置", "ConnectionReset", "EOF")):
        hint = "\n   → 连接被重置，说明该地址在境内被阻断，代理是必需的"
    elif "502" in joined:
        hint = "\n   → 502 通常是代理只放行部分域名，换成 Clash 等全量代理"

    raise URLError(f"订阅拉取失败: {joined}{hint}\n   url={url}")


def parse_subscription(content: str) -> List[NodeSpec]:
    """支持：base64 / 纯URI 列表 / JSON（v2rayN 格式）。"""
    content = (content or "").strip()
    if not content:
        return []

    # base64
    if re.fullmatch(r"[A-Za-z0-9+/=\s]+", content):
        try:
            decoded = base64.b64decode(content).decode("utf-8", errors="ignore")
            if any(k in decoded for k in ("://", '"')):
                return parse_subscription(decoded)
        except Exception:
            pass

    # JSON
    if content[0] in "{[":
        try:
            data = json.loads(content)
            items = data if isinstance(data, list) else data.get("outbounds", [])
            out = []
            for it in items:
                if not isinstance(it, dict):
                    continue
                n = NodeSpec(
                    type=str(it.get("protocol") or it.get("type") or "").lower(),
                    address=str(it.get("address") or it.get("server") or ""),
                    port=int(it.get("port") or it.get("server_port") or 0),
                    uuid=str(it.get("id") or it.get("uuid") or it.get("password") or ""),
                    password=str(it.get("password") or ""),
                    security=str(it.get("security") or it.get("method") or "auto"),
                    network=str(it.get("network") or it.get("net") or "tcp"),
                    path=str(it.get("path") or "/"),
                    host=str(it.get("host") or ""),
                    sni=str(it.get("sni") or it.get("servername") or ""),
                    tls=bool(it.get("tls")),
                    remark=str(it.get("remark") or it.get("ps") or it.get("tag") or ""),
                )
                if n.type and n.address:
                    out.append(n)
            if out:
                return out
        except Exception:
            pass

    # 按行
    nodes = []
    for line in content.splitlines():
        n = parse_uri(line)
        if n:
            nodes.append(n)
    return nodes


# --------------------------------------------------------------------------
# 漏斗执行
# --------------------------------------------------------------------------


def run_funnel(
    nodes: List[NodeSpec],
    *,
    use_kernel: bool = True,
    kernel: Optional[dict] = None,
    workers: int = 30,
    tcp_timeout: float = 4.0,
    tls_timeout: float = 5.0,
    kernel_timeout: float = 8.0,
    on_progress=None,
) -> List[NodeSpec]:
    """按五级漏斗逐级检测。"""
    total = len(nodes)
    done = 0

    def tick(stage: str):
        nonlocal done
        done += 1
        if on_progress:
            on_progress(done, total, stage)

    # ---- L1 解析（本地，最先做，能白砍一批）----
    survivors: List[NodeSpec] = []
    for n in nodes:
        ok, why = validate_spec(n)
        if ok:
            n.stage, n.reason = "L1", ""
            survivors.append(n)
        else:
            n.stage, n.reason = "L1", why
            tick("L1")

    # ---- L2 + L3（并发）----
    def probe(n: NodeSpec) -> NodeSpec:
        n.tcp_ok, n.tcp_latency = probe_tcp(n, timeout=tcp_timeout)
        if not n.tcp_ok:
            n.stage, n.reason = "L2", f"TCP不通 {n.address}:{n.port}"
            return n
        n.stage = "L2"
        if n.tls:
            n.tls_ok, n.tls_latency, why = probe_tls(n, timeout=tls_timeout)
            if not n.tls_ok:
                n.stage, n.reason = "L3", why
                return n
        n.stage = "L3"
        return n

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(probe, n): n for n in survivors}
        for fut in as_completed(futs):
            try:
                fut.result()
            except Exception:
                pass
            tick("L2/L3")

    l3_ok = [n for n in survivors if n.stage == "L3"]

    # ---- L4 内核端到端（★ 权威判据）----
    if use_kernel and kernel and l3_ok:
        def kv(n: NodeSpec) -> NodeSpec:
            r = verify_node(n, kernel, timeout=kernel_timeout)
            n.kernel_ok = r.ok
            n.kernel_reason = r.reason
            n.kernel_http_status = r.http_status
            n.kernel_version = r.kernel
            if r.ok:
                n.stage, n.valid = "L4", True
                n.reason = r.reason
            else:
                n.stage, n.reason = "L4", r.reason
            return n

        with ThreadPoolExecutor(max_workers=max(4, workers // 4)) as ex:
            futs = {ex.submit(kv, n): n for n in l3_ok}
            for fut in as_completed(futs):
                try:
                    fut.result()
                except Exception:
                    pass
                tick("L4")
    else:
        # 未启用内核：L3 通过者视为"初判可用"，但明确标记未做真实验证
        for n in l3_ok:
            n.stage, n.valid = "L3", True
            n.reason = "仅 TCP+TLS 通过（未做真实代理验证，可能不可用）"

    return nodes


def run_speed(
    nodes: List[NodeSpec],
    kernel: dict,
    top_n: int = 10,
    seconds: float = 6.0,
) -> None:
    """只对前 top_n 名做隧道测速（串行，避免互相抢带宽）。"""
    pool = [n for n in nodes if n.kernel_ok][:top_n]
    for n in pool:
        r = speed_test(n, kernel, seconds=seconds)
        if r.get("ok"):
            n.speed_mode = r.get("measured", "bandwidth")
            n.mbps = r.get("mbps", 0.0)
            n.speed_bytes = r.get("bytes", 0)
            n.jitter_ms = r.get("jitter_ms", 0.0)
            n.stage = "L5"
            if n.speed_mode == "rtt" and r.get("note"):
                n.reason = r["note"]
        else:
            n.reason += f" | 测速失败: {r.get('reason','')}"


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------


def _probe_proxy(proxy: str, timeout: float = 8.0) -> bool:
    """检查代理是否真的能出网。"""
    import urllib.request as _u
    opener = _u.build_opener(_u.ProxyHandler({"http": proxy, "https": proxy}))
    try:
        req = _u.Request("https://www.gstatic.com/generate_204",
                         headers={"User-Agent": "Mozilla/5.0"})
        with opener.open(req, timeout=timeout) as r:
            print(f"✅ 代理可用（HTTP {r.status}）")
            return True
    except Exception as exc:
        print(f"   原因：{type(exc).__name__}: {str(exc)[:60]}")
        return False


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="节点检测：五级漏斗 + 真实内核验证 + 评分排序",
    )
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--url", action="append", help="订阅链接（可多次）")
    src.add_argument("--file", help="节点文件（每行一个 URI）")
    src.add_argument("--paste", action="store_true", help="从标准输入读取")

    ap.add_argument("--kernel", default="auto",
                    choices=["auto", "sing-box", "xray"])
    ap.add_argument("--no-kernel", action="store_true",
                    help="只跑到 L3（快，但有假阳性）")
    ap.add_argument("--speed", action="store_true", help="对 TOP N 做隧道测速")
    ap.add_argument("--top", type=int, default=10, help="测速节点数（默认 10）")
    ap.add_argument("--speed-seconds", type=float, default=6.0)
    ap.add_argument("--workers", type=int, default=30)
    ap.add_argument("--kernel-timeout", type=float, default=8.0)
    ap.add_argument("--out", default="result", help="输出目录")
    ap.add_argument("--json", help="JSON 报告路径")
    ap.add_argument(
        "--proxy",
        default="http://127.0.0.1:10808",
        help="拉取订阅用的本地代理（默认 Clash 10808；填 none 表示直连）",
    )
    args = ap.parse_args(argv)

    t0 = time.time()

    # ---- 1. 收集节点 ----
    nodes: List[NodeSpec] = []
    if args.url:
        proxy = None if str(args.proxy).lower() == "none" else args.proxy

        # ---- 前置依赖提示（见下方说明）----
        if proxy is None:
            print("🔍 未指定代理，尝试直连拉取订阅")
            print("   （GitHub Raw 等境外源在境内通常无法直连）")
        else:
            print(f"🔍 检查代理 {proxy} ...")
            if not _probe_proxy(proxy):
                print("")
                print("⚠️ 检测存在前置依赖，原理如下：")
                print("   项目内的 sing-box/xray 是通用引擎，本身【不含节点】")
                print("   ① 拉取订阅 → 需要一个【已能上网的外部代理】")
                print("   ② 检测节点 → 把 ① 拿到的节点喂给内核验证")
                print("   若 ① 失败，② 必然是 0 个节点 —— 这不是工具故障")
                print("")
                print("💡 解决办法（三选一）：")
                print("   a) 启动 v2rayN / Clash 等，本工具 --proxy 填它的端口")
                print("   b) 用 --file / --paste 直接粘贴节点 URI，完全不需要代理")
                print("   c) --proxy none 走直连 —— 仅当订阅源在境内可达时")
                return 3

        for u in args.url:
            try:
                content = fetch_subscription(u, proxy=proxy)
                got = parse_subscription(content)
                print(f"订阅 {u[:60]}... → {len(got)} 个节点")
                nodes.extend(got)
            except Exception as exc:
                print(f"❌ 订阅拉取失败 {u[:60]}: {exc}", file=sys.stderr)
    elif args.file:
        text = Path(args.file).read_text(encoding="utf-8", errors="ignore")
        nodes = parse_subscription(text)
        print(f"文件 {args.file} → {len(nodes)} 个节点")
    else:
        text = sys.stdin.read()
        nodes = parse_subscription(text)
        print(f"标准输入 → {len(nodes)} 个节点")

    if not nodes:
        print("没有可解析的节点。", file=sys.stderr)
        return 2

    # ---- 2. 内核 ----
    kernel = None
    if not args.no_kernel:
        try:
            kernel = find_kernel(args.kernel)
            print(f"内核: {kernel['kind']} {kernel['version']}")
        except KernelNotFound as exc:
            print(f"⚠️ {exc}\n   → 回退到仅 TCP+TLS 模式（结果含假阳性）",
                  file=sys.stderr)
            args.no_kernel = True

    # ---- 3. 跑漏斗 ----
    print(f"\n开始检测 {len(nodes)} 个节点（并发 {args.workers}）...")
    last = [0.0]

    def progress(done: int, total: int, stage: str):
        now = time.time()
        if now - last[0] > 0.4 or done == total:
            last[0] = now
            print(f"\r  {done}/{total}  [{stage}]", end="", flush=True)

    nodes = run_funnel(
        nodes,
        use_kernel=not args.no_kernel,
        kernel=kernel,
        workers=args.workers,
        kernel_timeout=args.kernel_timeout,
        on_progress=progress,
    )
    print()

    # ---- 4. 测速 ----
    if args.speed and kernel:
        print(f"\n隧道测速（TOP {args.top}，每节点 {args.speed_seconds:.0f}s）...")
        run_speed(nodes, kernel, top_n=args.top, seconds=args.speed_seconds)
        print("测速完成")

    # ---- 5. 评分排序 ----
    nodes = apply_scores(nodes)
    working = [n for n in nodes if n.kernel_ok]

    elapsed = time.time() - t0

    # ---- 6. 打印报告 ----
    print("\n" + "=" * 68)
    print("漏斗统计")
    print("=" * 68)
    for s in ("L1", "L2", "L3", "L4", "L5"):
        c = sum(1 for n in nodes if n.stage == s)
        if c:
            print(f"  {s}: {c}")
    print(f"  TCP 通: {sum(1 for n in nodes if n.tcp_ok)}"
          f" | TLS 通: {sum(1 for n in nodes if n.tls_ok)}")
    print(f"  ★ 真实可用（内核验证通过）: {len(working)}")

    if working:
        print("\n" + "=" * 68)
        print(f"TOP {min(20, len(working))} 可用节点")
        print("=" * 68)
        print(f"{'排名':<4}{'节点':<30}{'评分':>6}{'延迟':>8}{'抖动':>8}{'速度':>9}")
        print("-" * 72)
        for i, n in enumerate(working[:20], 1):
            label = n.label[:28]
            spd = f"{n.mbps:.1f}M" if n.mbps else "--"
            jit = f"±{n.jitter_ms:.0f}ms" if n.jitter_ms else "--"
            print(f"{i:<5}{label:<30}{n.score:>6.0f}{n.latency:>7.0f}ms{jit:>8}{spd:>9}")

    # 失败原因分布
    failed = [n for n in nodes if not n.valid]
    if failed:
        print("\n" + "=" * 68)
        print("失败原因 TOP 8")
        print("=" * 68)
        from collections import Counter
        c = Counter()
        for n in failed:
            r = n.reason or ""
            if "TCP" in r:
                key = "TCP 不通"
            elif "TLS" in r:
                key = "TLS 握手失败"
            elif "CONNECT 被拒" in r:
                key = "隧道握手被拒（节点已失效/参数错）"
            elif "超时" in r:
                key = "请求超时"
            else:
                key = r[:40] or "未知"
            c[key] += 1
        for k, v in c.most_common(8):
            print(f"  {v:>5}  {k}")

    # ---- 7. 输出文件 ----
    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")

    if working:
        sub_file = outdir / f"sub_{stamp}.txt"
        sub_file.write_text(to_v2rayn_subscription(nodes), encoding="utf-8")
        plain_file = outdir / f"valid_{stamp}.txt"
        plain_file.write_text(to_plain_list(nodes), encoding="utf-8")
        print(f"\n✅ v2rayN 订阅: {sub_file}")
        print(f"✅ 明文列表  : {plain_file}")
    else:
        print("\n⚠️ 没有通过真实内核验证的节点。")
        if args.no_kernel:
            print("   提示：加 --speed 并去掉 --no-kernel 可做真实验证。")

    rep = build_report(
        nodes,
        kernel_version=(
            f"{kernel['kind']} {kernel['version']}" if kernel else "未启用"
        ),
        elapsed=elapsed,
    )
    jf = Path(args.json) if args.json else outdir / f"report_{stamp}.json"
    jf.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"✅ JSON 报告 : {jf}")
    print(f"\n耗时 {elapsed:.1f}s")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())