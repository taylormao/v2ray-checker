"""
eventlet 适配层：把漏斗改成绿色线程池并发
============================================

为什么要单独适配
----------------
`cli.run_funnel` 用 `ThreadPoolExecutor`。在 eventlet 环境下有两个问题：

  1) 它已被 monkey_patch 成绿色线程池，但 L4 内核级检测每个要占住
     一个 socket 5~8 秒 —— 用绿色线程做阻塞式并发等于**串行**，
     表现为界面长时间无响应。
  2) 标准 `threading.Lock/Event` 在请求线程里用会抛
     `RuntimeError: Working outside of application context`。

解法：用 `Semaphore` 做并发闸门 + `spawn` 起绿色线程，
完成即回调，从而能边完成边推进度条。

注意：`GreenPool` 只有**有序**的 `imap`（会阻塞等待结果），
没有 `imap_unordered`，所以「完成即回调」必须自己实现。
"""

from __future__ import annotations

from typing import Callable, List, Optional

# ⚠️ 必须在 eventlet.monkey_patch() **之后**从 eventlet 顶层导入。
#    monkey_patch 会把标准库 threading/socket 换成绿色版本，
#    此时再从 eventlet.green.* 深层路径导入会拿到已被patch 的模块，
#    表现为 `cannot import name 'spawn' from 'eventlet.green.thread'`。
from eventlet import Event, GreenPool, Semaphore, spawn

from .kernel_probe import verify_node
from .pipeline import NodeSpec, probe_tcp, probe_tls, validate_spec


def _parallel_map(
    func: Callable[[NodeSpec], NodeSpec],
    items: List[NodeSpec],
    size: int,
    on_each_done: Callable[[], None],
    stop_flag=None,
) -> None:
    """执行 func(items)，并发上限 size，每完成一个调on_each_done。

    用 Semaphore 做闸门限制并发数；spawn 起绿色线程；
    最后 join 全部，保证函数返回时所有结果都已写入节点对象。
    """
    if not items:
        return

    sem = Semaphore(size)
    all_done = Event()
    counter = [0]
    total = len(items)
    # 计数锁：并发下counter[0] += 1 不是原子操作，会丢更新导致 all_done 永不触发
    clk = Semaphore(1)

    def _should_run() -> bool:
        """停止信号是否允许继续。兼容 eventlet.Event 与 threading.Event。"""
        if stop_flag is None:
            return True
        if hasattr(stop_flag, "ready"):        # eventlet.Event
            return not stop_flag.ready()
        return not stop_flag.is_set()           # threading.Event

    def runner(n: NodeSpec) -> None:
        try:
            sem.acquire()
            try:
                if _should_run():
                    func(n)
            finally:
                sem.release()
        except Exception:
            pass                  # 单个节点失败不能影响整体
        finally:
            on_each_done()
            with clk:
                counter[0] += 1
                if counter[0] >= total:
                    all_done.send()

    # ⚠️ 不能「主线程串行 acquire → 再 spawn」—— 那会死锁：
    #    前 size 个占满信号量后主线程阻塞在 acquire，而释放依赖
    #    已被 spawn 的任务跑完，任务却还没被 spawn → 循环等待。
    # 正确做法：一次 spawn 全部，由信号量在函数内部限流。
    threads = [spawn(runner, n) for n in items]

    all_done.wait()
    for t in threads:
        try:
            t.wait()
        except Exception:
            pass


def run_funnel_green(
    nodes: List[NodeSpec],
    *,
    use_kernel: bool = True,
    kernel: Optional[dict] = None,
    workers: int = 30,
    tcp_timeout: float = 4.0,
    tls_timeout: float = 5.0,
    kernel_timeout: float = 8.0,
    stop_flag=None,
    on_progress: Optional[Callable[[int, int, str], None]] = None,
) -> List[NodeSpec]:
    """eventlet 版五级漏斗。行为与 cli.run_funnel 一致。"""
    total = len(nodes)
    state = [0]

    def tick(stage: str) -> None:
        state[0] += 1
        if on_progress:
            on_progress(state[0], total, stage)

    # ---- L1 解析（纯本地，零成本）----
    survivors: List[NodeSpec] = []
    for n in nodes:
        ok, why = validate_spec(n)
        if ok:
            n.stage, n.reason = "L1", ""
            survivors.append(n)
        else:
            n.stage, n.reason = "L1", why
            tick("L1")

    if not survivors:
        return nodes

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

    _parallel_map(probe, survivors, size=workers,
                  on_each_done=lambda: tick("L2/L3"))

    l3_ok = [n for n in survivors if n.stage == "L3"]

    # ---- L4 内核端到端（★ 权威判据）----
    # 并发压低：每个实例都会起一个内核进程并占端口，过并发会拖垮机器
    if use_kernel and kernel and l3_ok:
        def kv(n: NodeSpec) -> NodeSpec:
            # ⚠️ 不能用 stop_flag.is_set() —— eventlet.Event 没有这个属性，
            # 会抛 AttributeError 被外层 except 吞掉，导致节点卡在 L3 不进 L4。
            if stop_flag is not None:
                stopped = (stop_flag.ready()
                           if hasattr(stop_flag, "ready")
                           else stop_flag.is_set())
                if stopped:
                    n.stage, n.reason = "L4", "用户中止"
                    return n
            r = verify_node(n, kernel, timeout=kernel_timeout)
            n.kernel_ok = r.ok
            n.kernel_reason = r.reason
            n.kernel_http_status = r.http_status
            n.kernel_version = r.kernel
            if r.ok:
                n.stage, n.valid, n.reason = "L4", True, r.reason
                # 内核级耗时比 TCP 探测更能反映真实体感延迟
                if r.elapsed_ms:
                    n.tcp_latency = r.elapsed_ms
            else:
                n.stage, n.reason = "L4", r.reason
            return n

        _parallel_map(kv, l3_ok, size=max(4, workers // 6),
                      on_each_done=lambda: tick("L4"), stop_flag=stop_flag)
    else:
        for n in l3_ok:
            n.stage, n.valid = "L3", True
            n.reason = "仅 TCP+TLS 通过（未做真实代理验证，可能不可用）"

    return nodes