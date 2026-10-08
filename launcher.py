"""
启动器：在独立（守护）进程中运行 app_v2.py。

为什么需要它
------------
`start_v2.bat` 直接跑 `python app_v2.py` 时有两个问题：

1. **窗口关闭服务就停** —— 用户随手关掉黑窗口，服务也没了
2. **无法同时做到「后台运行」+「让批处理知道何时就绪」**

而常见的替代方案都不可靠：

- `start /min`   —— 子进程仍属于同一作业对象，父进程被杀时连带被杀
- `wmic process call create` —— 部分环境禁用 wmic（安全策略）
- `powershell Start-Process` —— 在受限环境里也可能被拦

本脚本用 `subprocess.DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP`
创建**独立进程**，不依赖任何外部命令，且能保证服务在父进程退出后继续运行。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def main() -> int:
    """以独立进程启动 app_v2.py。"""
    root = Path(__file__).resolve().parent
    app = root / "app_v2.py"
    python = Path(sys.executable)

    if not app.exists():
        print(f"[错误] 找不到 {app}", file=sys.stderr)
        return 2

    # Windows 专有标志；非 Windows 退化为普通启动（便于跨平台调试）
    flags = 0
    if sys.platform == "win32":
        flags = (
            subprocess.DETACHED_PROCESS      # 独立进程，父进程退出不受影响
            | subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.CREATE_NO_WINDOW    # 不弹黑框
        )

    env = dict(**__import__("os").environ)
    env["PYTHONIOENCODING"] = "utf-8"

    # 日志写文件而非 DEVNULL —— 出问题时用户能看到真实报错
    log_dir = root / "logs"
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / "server.log"

    try:
        lf = open(log_file, "ab", buffering=0)
        subprocess.Popen(
            [str(python), str(app)],
            cwd=str(root),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=lf,                     # 不继承控制台，避免管道关闭时崩溃
            stderr=subprocess.STDOUT,
            creationflags=flags,
            close_fds=True,
        )
        lf.close()
    except Exception as exc:
        print(f"[错误] 启动失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"[错误] 启动失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())