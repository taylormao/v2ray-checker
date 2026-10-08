"""
启动前的端口就绪探测小工具（供 start_v2.bat 调用）。

为什么需要它
------------
批处理里没有可靠的"判断 TCP 端口是否已监听"的内建命令。
常见的几种替代都有问题：
  - `netstat | findstr` 只能看连接，看不出服务是否已能接受请求
  - 调用 PowerShell 的 Test-NetConnection —— 在 Git Bash / 受限环境里
    可能被安全策略拦截，而且每次调用要 1~2 秒，轮询十几秒会明显变慢
  - `ping` 测的是 ICMP，跟端口无关

所以用一个极小的 Python 脚本：连接被接受即 exit 0，否则 exit 1。
启动一次仅 ~10ms，轮询 15 次也才150ms，开销可忽略。
"""

import socket
import sys


def main() -> int:
    if len(sys.argv) < 2:
        print("用法: wait_port.py <port> [timeout_sec]", file=sys.stderr)
        return 2

    port = int(sys.argv[1])
    timeout = float(sys.argv[2]) if len(sys.argv) > 2 else 1.0

    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return 0          # 端口已接受连接 → 就绪
    except OSError:
        return 1              # 还没起来


if __name__ == "__main__":
    raise SystemExit(main())