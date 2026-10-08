"""
Web UI 后端 —— 真内核验证版
============================

与 1.0 版的关键差异
--------------------
1.0 版：subprocess 拉起 check_subscription.py，再解析 stdout 的进度文本。
        天然缺陷 —— 只能显示「已检测 N 个」，看不到【测速排名/失败原因分布】，
        且内核级验证根本没接入。

2.0 版：直接在进程内调用 pipeline.run_funnel / kernel_probe，
        通过 Socket.IO 实时推送阶段化进度与结构化结果。

对外契约（与前端模板完全兼容，模板无需改动）
--------------------------------------------
事件  log      : {"message": str, "level": "info|success|warning|error"}
事件  status   : 与 1.0 版相同的 status 结构
接口  POST /api/check        {urls: [str], timeout, workers, speed, top}
接口  POST /api/check_nodes  {nodes: str, timeout, workers, speed, top}
接口  POST /api/stop
接口  GET  /api/status
接口  GET  /api/results
接口  GET  /api/result/<filename>
接口  POST /api/clear_results
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import eventlet

eventlet.monkey_patch()      # 必须在导入任何标准库网络模块之前

from eventlet import Event as _Evt, Semaphore as _Sem, spawn as _gspawn  # noqa: E402
from flask import Flask, jsonify, render_template, request, send_file     # noqa: E402
from flask_socketio import SocketIO, emit# noqa: E402

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR / "src"))

from v2ray_checker_1_0_0.cli import (          # noqa: E402
    fetch_subscription, parse_subscription,
)
from v2ray_checker_1_0_0.web_funnel import run_funnel_green  # noqa: E402
from v2ray_checker_1_0_0.kernel_probe import (  # noqa: E402
    KernelNotFound, find_kernel, speed_test,
)
from v2ray_checker_1_0_0.pipeline import (     # noqa: E402
    apply_scores, build_report, to_plain_list, to_v2rayn_subscription,
)

app = Flask(__name__)
app.config["SECRET_KEY"] = "v2ray-checker-secret-key"
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")

RESULT_DIR = BASE_DIR / "result"
RESULT_DIR.mkdir(exist_ok=True)
TEMP_DIR = BASE_DIR / "temp"
TEMP_DIR.mkdir(exist_ok=True)

# --------------------------------------------------------------------------
# 全局状态
# --------------------------------------------------------------------------

# ⚠️ 必须用 eventlet 原生原语，不能用标准 threading.Lock/Event：
#    eventlet 已monkey_patch 掉 threading，标准对象与其绿色线程模型不兼容，
#    在请求线程里加锁会抛 RuntimeError 并拖垮整个服务。
_stop_flag = _Evt()
_lock = _Sem(1)

checking_status: Dict[str, Any] = {
    "is_running": False,
    "progress": 0,
    "total_nodes": 0,
    "checked_nodes": 0,
    "valid_nodes": 0,
    "invalid_nodes": 0,
    "current_node": "",
    "log_messages": [],
    "start_time": None,
    "end_time": None,
}

# 本次检测的完整结果（供 /api/results 使用）
last_result: Dict[str, Any] = {
    "funnel": {},
    "top": [],
    "failure_reasons": [],
    "report_path": "",
    "sub_path": "",
    "plain_path": "",
    "kernel": "",
    "mode": "",
}


def reset_status() -> None:
    global checking_status
    checking_status = {
        "is_running": True,
        "progress": 0,
        "total_nodes": 0,
        "checked_nodes": 0,
        "valid_nodes": 0,
        "invalid_nodes": 0,
        "current_node": "",
        "log_messages": [],
        "start_time": datetime.now().isoformat(),
        "end_time": None,
    }
    # ⚠️ eventlet.Event 没有 clear()（标准 threading.Event 才有），
    # 直接调会 AttributeError 并让整个服务挂掉 —— 只能换一个新实例。
    global _stop_flag
    _stop_flag = _Evt()


def add_log(message: str, level: str = "info") -> None:
    """推送日志（同时保留在 status 里供断线重连后补看）。

    ⚠️ 必须带 timestamp 字段 —— 前端 addLog(timestamp, message, level)
    是三参数签名，缺了它每条日志都会显示成 undefined，表现为「日志区域空白」。
    这是 1.0 前端契约，后端要严格对齐。
    """
    ts = datetime.now().strftime("%H:%M:%S")
    with _lock:
        checking_status["log_messages"].append({
            "time": ts,
            "timestamp": ts,
            "message": message,
            "level": level,
        })
        if len(checking_status["log_messages"]) > 2000:
            checking_status["log_messages"] = checking_status["log_messages"][-2000:]
    socketio.emit("log", {
        "timestamp": ts,
        "time": ts,
        "message": message,
        "level": level,
    })


def push_status(**kw: Any) -> None:
    with _lock:
        checking_status.update(kw)
    socketio.emit("status", checking_status)


# --------------------------------------------------------------------------
# 路由
# --------------------------------------------------------------------------


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/check", methods=["POST"])
def start_check():
    data = request.get_json(silent=True) or {}
    urls = [u.strip() for u in data.get("urls", []) if u.strip()]
    if not urls:
        return jsonify({"success": False, "message": "请填写订阅链接"}), 400

    opts = _extract_opts(data)
    if checking_status["is_running"]:
        return jsonify({"success": False, "message": "已有检测任务在运行"}), 409

    _gspawn(_run_by_url, urls, opts)
    return jsonify({"success": True, "message": f"已开始检测 {len(urls)} 个订阅"})


@app.route("/api/check_nodes", methods=["POST"])
def start_nodes_check():
    data = request.get_json(silent=True) or {}
    text = data.get("nodes", "")
    if not text.strip():
        return jsonify({"success": False, "message": "请粘贴节点"}), 400

    opts = _extract_opts(data)
    if checking_status["is_running"]:
        return jsonify({"success": False, "message": "已有检测任务在运行"}), 409

    _gspawn(_run_by_nodes, text, opts)
    return jsonify({"success": True, "message": "已开始检测"})


def _extract_opts(data: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "timeout": float(data.get("timeout") or 4),
        "workers": int(data.get("workers") or 30),
        "kernel_timeout": float(data.get("kernel_timeout") or 8),
        "speed": bool(data.get("speed", False)),
        "top": int(data.get("top") or 10),
        "speed_seconds": float(data.get("speed_seconds") or 6),
        "proxy": data.get("proxy") or "http://127.0.0.1:10808",
    }


@app.route("/api/stop", methods=["POST"])
def stop_check():
    if not checking_status["is_running"]:
        return jsonify({"success": False, "message": "当前没有运行中的任务"})
    # eventlet.Event 的置位方法是 send()，不是 set()
    if not _stop_flag.ready():
        _stop_flag.send()
    add_log("⏹️ 已请求停止，当前节点检测完后终止", "warning")
    return jsonify({"success": True, "message": "正在停止"})


@app.route("/api/status")
def get_status():
    return jsonify(checking_status)


@app.route("/api/results")
def get_results():
    """结构化检测结果（内核面板数据）。"""
    return jsonify(last_result)


@app.route("/api/result_files")
def get_result_files():
    """结果文件列表（供下载）。与结构化结果分开，两个关注点互不干扰。"""
    files = []
    for f in sorted(RESULT_DIR.glob("*"), key=lambda x: -x.stat().st_mtime):
        if f.is_file():
            files.append({
                "name": f.name,
                "size": f.stat().st_size,
                "modified": datetime.fromtimestamp(f.stat().st_mtime).isoformat(),
            })
    return jsonify(files)


@app.route("/api/result/<path:filename>")
def download_result(filename: str):
    target = (RESULT_DIR / filename).resolve()
    # 防目录穿越
    if not str(target).startswith(str(RESULT_DIR.resolve())):
        return jsonify({"success": False, "message": "非法路径"}), 400
    if not target.exists():
        return jsonify({"success": False, "message": "文件不存在"}), 404
    return send_file(target, as_attachment=True)


@app.route("/api/clear_results", methods=["POST"])
def clear_results():
    global last_result
    for f in RESULT_DIR.glob("*"):
        try:
            f.unlink()
        except OSError:
            pass
    last_result = {
        "funnel": {}, "top": [], "failure_reasons": [],
        "report_path": "", "sub_path": "", "plain_path": "",
        "kernel": "", "mode": "",
    }
    return jsonify({"success": True, "message": "已清空结果"})


@socketio.on("connect")
def handle_connect():
    emit("status", checking_status)


# --------------------------------------------------------------------------
# 核心执行
# --------------------------------------------------------------------------


def _collect(urls: List[str], proxy: Optional[str]) -> List:
    nodes = []
    for u in urls:
        if _stop_flag.ready():
            break
        add_log(f"📥 拉取订阅 {u[:56]}", "info")
        try:
            content = fetch_subscription(u, proxy=proxy)
            got = parse_subscription(content)
            add_log(f"✅ 解析 {len(got)} 个节点", "success")
            nodes.extend(got)
        except Exception as exc:
            add_log(f"❌ 订阅失败：{exc}", "error")
    return nodes


def _on_progress(done: int, total: int, stage: str) -> None:
    pct = int(done * 100 / total) if total else 0
    push_status(
        progress=pct,
        checked_nodes=done,
        total_nodes=total,
        current_node=stage,
    )


def _classify_failure(nodes: List) -> List[Dict[str, Any]]:
    """把失败原因聚合成排行，供前端展示。"""
    c: Counter = Counter()
    for n in nodes:
        if n.valid:
            continue
        r = n.reason or ""
        if "TCP" in r:
            key = "TCP 不通"
        elif "TLS" in r:
            key = "TLS 握手失败"
        elif "CONNECT 被拒" in r:
            key = "隧道握手被拒（节点失效或参数错）"
        elif "无响应" in r:
            key = "隧道建立但目标无响应（出口受限）"
        elif "超时" in r:
            key = "请求超时"
        elif "uuid" in r:
            key = "uuid 格式非法"
        elif "未就绪" in r or "启动即退出" in r:
            key = "内核未能启动"
        else:
            key = (r[:36] or "未知")
        c[key] += 1
    return [
        {"reason": k, "count": v} for k, v in c.most_common(10)
    ]


def _finalize(nodes: List, kernel_info: Dict[str, Any], opts: Dict[str, Any],
              mode: str) -> None:
    """打分、排序、写文件、推送最终结果。"""
    nodes = apply_scores(nodes)
    working = [n for n in nodes if n.valid]

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    # ---- TOP 排行 ----
    top = []
    for i, n in enumerate(working[:50], 1):
        top.append({
            "rank": i,
            "label": n.label,
            "address": n.address,
            "port": n.port,
            "type": n.type,
            "network": n.network,
            "score": n.score,
            "latency": round(n.latency, 1),
            "jitter": round(n.jitter_ms, 1),
            "mbps": n.mbps,
            "speed_mode": n.speed_mode,
            "remark": n.remark,
        })

    # ---- 写文件 ----
    sub_name = plain_name = rep_name = ""
    if working:
        sub_name = f"sub_{stamp}.txt"
        (RESULT_DIR / sub_name).write_text(
            to_v2rayn_subscription(nodes), encoding="utf-8")
        plain_name = f"valid_{stamp}.txt"
        (RESULT_DIR / plain_name).write_text(
            to_plain_list(nodes), encoding="utf-8")

    rep_name = f"report_{stamp}.json"
    report = build_report(nodes, kernel_version=kernel_info.get("version_str", ""),
                          elapsed=0.0)
    (RESULT_DIR / rep_name).write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    global last_result
    last_result = {
        "funnel": report["funnel"],
        "summary": report["summary"],
        "top": top,
        "failure_reasons": _classify_failure(nodes),
        "report_path": rep_name,
        "sub_path": sub_name,
        "plain_path": plain_name,
        "kernel": kernel_info.get("version_str", ""),
        "mode": mode,
    }

    # ---- 日志摘要 ----
    add_log("─" * 46, "info")
    add_log(f"总节点 {len(nodes)}｜★真实可用 {len(working)}", "success")
    add_log(f"TCP 通 {report['summary']['tcp_ok']}｜TLS 通 {report['summary']['tls_ok']}",
            "info")
    if working:
        add_log(f"🏆 TOP1 {working[0].label} 评分 {working[0].score:.0f}"
                f"（{working[0].latency:.0f}ms）", "success")
    for fr in last_result["failure_reasons"][:5]:
        add_log(f"  × {fr['count']:>4} {fr['reason']}", "warning")

    push_status(
        progress=100,
        checked_nodes=len(nodes),
        total_nodes=len(nodes),
        valid_nodes=len(working),
        invalid_nodes=len(nodes) - len(working),
        current_node="done",
    )
    socketio.emit("results", last_result)


def _run_by_url(urls: List[str], opts: Dict[str, Any]) -> None:
    global last_result
    reset_status()
    t0 = time.time()
    add_log("🚀 开始订阅检测（内核验证模式）", "success")

    proxy = None if str(opts["proxy"]).lower() == "none" else opts["proxy"]
    nodes = _collect(urls, proxy)
    if not nodes:
        add_log("❌ 未取到任何节点，任务结束", "error")
        push_status(is_running=False, end_time=datetime.now().isoformat())
        return

    push_status(total_nodes=len(nodes))
    _execute(nodes, opts, "url", t0)


def _run_by_nodes(text: str, opts: Dict[str, Any]) -> None:
    reset_status()
    t0 = time.time()
    add_log("🚀 开始节点检测（内核验证模式）", "success")

    nodes = parse_subscription(text)
    if not nodes:
        add_log("❌ 没有解析到有效节点，请检查格式", "error")
        push_status(is_running=False, end_time=datetime.now().isoformat())
        return

    add_log(f"✅ 解析 {len(nodes)} 个节点", "success")
    push_status(total_nodes=len(nodes))
    _execute(nodes, opts, "nodes", t0)


def _execute(nodes: List, opts: Dict[str, Any], mode: str, t0: float) -> None:
    # ---- 内核 ----
    kernel = None
    try:
        kernel = find_kernel("auto")
        version_str = f"{kernel['kind']} {kernel['version']}"
        add_log(f"内核：{version_str}", "info")
    except KernelNotFound as exc:
        add_log(f"⚠️ {exc}", "warning")
        add_log("回退到「仅 TCP+TLS」模式 —— 结果含假阳性", "warning")
        kernel = {"kind": "", "path": "", "version": "", "version_str": "未启用"}

    if not kernel["path"]:
        # 无内核：只跑到 L3
        for n in nodes:
            n.stage, n.reason = "L3", "未启用内核验证"
        kernel_info = {"version_str": "未启用"}
    else:
        kernel_info = {"version_str": f"{kernel['kind']} {kernel['version']}"}

    # ---- 漏斗 ----
    def progress_cb(done: int, total: int, stage: str) -> None:
        _on_progress(done, total, stage)

    nodes = run_funnel_green(
        nodes,
        use_kernel=bool(kernel["path"]),
        kernel=kernel if kernel["path"] else None,
        workers=opts["workers"],
        kernel_timeout=opts["kernel_timeout"],
        stop_flag=_stop_flag,
        on_progress=progress_cb,
    )

    # ---- 测速（仅 TOP N）----
    if opts["speed"] and kernel.get("path"):
        pool = [n for n in nodes if n.valid][:opts["top"]]
        if pool:
            add_log(f"⏱️  测速 TOP {len(pool)}（每节点 {opts['speed_seconds']:.0f}s）",
                    "info")
            for n in pool:
                if _stop_flag.ready():
                    add_log("⏹️ 测速已中止", "warning")
                    break
                r = speed_test(n, kernel, seconds=opts["speed_seconds"])
                if r.get("ok"):
                    n.speed_mode = r.get("measured", "bandwidth")
                    n.mbps = r.get("mbps", 0.0)
                    n.speed_bytes = r.get("bytes", 0)
                    n.jitter_ms = r.get("jitter_ms", 0.0)
                    n.stage = "L5"
                    if n.speed_mode == "rtt" and r.get("note"):
                        add_log(f"  {n.label[:30]} {r['note']}", "info")
                    else:
                        add_log(f"  {n.label[:30]} {r.get('mbps', 0):.2f} Mbps", "success")
                else:
                    add_log(f"  {n.label[:30]} 测速失败：{r.get('reason','')}",
                            "warning")

    elapsed = time.time() - t0
    _finalize(nodes, kernel_info, opts, mode)

    with _lock:
        _ts = datetime.now().strftime("%H:%M:%S")
        checking_status["log_messages"].append({
            "time": _ts, "timestamp": _ts,
            "message": f"⏱️ 总耗时 {elapsed:.1f}s",
            "level": "info",
        })

    push_status(is_running=False, end_time=datetime.now().isoformat())
    add_log(f"⏱️ 总耗时 {elapsed:.1f}s", "info")


if __name__ == "__main__":
    print("启动节点检测服务：http://localhost:5000")
    # ⚠️ eventlet 后端不认 allow_unsafe_werkzeug（那是 werkzeug 后端的参数），
    # 传了会报 `server() got an unexpected keyword argument`。
    socketio.run(app, host="0.0.0.0", port=5000, debug=False)