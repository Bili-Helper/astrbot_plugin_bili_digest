"""Cancellable subprocesses with bounded output and no command shell."""

import asyncio
import os
import signal


class DigestError(Exception):
    pass


class LimitError(DigestError):
    pass


async def run_process(args, timeout, cwd=None, env=None, monitor_dir=None, max_bytes=0):
    options = {"start_new_session": True} if os.name != "nt" else {"creationflags": 0x08000000}
    try:
        proc = await asyncio.create_subprocess_exec(
            *map(str, args),
            cwd=cwd,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **options,
        )
    except FileNotFoundError:
        raise DigestError("找不到所需程序，请检查下载器、ffmpeg 或 Python 依赖设置。") from None
    overflow = False

    async def drain(stream, keep, fatal=False):
        nonlocal overflow
        data = bytearray()
        while chunk := await stream.read(65536):
            if len(data) + len(chunk) > keep:
                if fatal:
                    overflow = True
                data.extend(chunk[: max(0, keep - len(data))])
            else:
                data.extend(chunk)
        return bytes(data)

    stdout = asyncio.create_task(drain(proc.stdout, 32 * 1024 * 1024, True))
    stderr = asyncio.create_task(drain(proc.stderr, 128 * 1024))
    waiter = asyncio.create_task(proc.wait())

    async def monitor():
        while not waiter.done():
            await asyncio.wait({waiter}, timeout=0.25)
            if overflow:
                raise DigestError("外部程序返回内容过大。")
            if monitor_dir and max_bytes:
                total = 0
                for path in monitor_dir.rglob("*"):
                    try:
                        if path.is_file():
                            total += path.stat().st_size
                    except FileNotFoundError:
                        continue
                if total > max_bytes:
                    raise LimitError("临时文件超过配置的下载大小限制。")
        await waiter
        await asyncio.gather(stdout, stderr)
        if overflow:
            raise DigestError("外部程序返回内容过大。")

    try:
        await asyncio.wait_for(monitor(), timeout)
        if proc.returncode:
            # Stderr may contain credentials and signed URLs; expose only the exit code.
            raise DigestError(
                f"外部程序执行失败（退出码 {proc.returncode}），请检查依赖、Cookie、模型或平台访问限制。"
            )
        return stdout.result().decode("utf-8", errors="replace")
    finally:
        if proc.returncode is None:
            if os.name == "nt":
                killer = await asyncio.create_subprocess_exec(
                    "taskkill",
                    "/PID",
                    str(proc.pid),
                    "/T",
                    "/F",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                    creationflags=0x08000000,
                )
                await killer.wait()
                if proc.returncode is None:
                    proc.kill()
            else:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        await asyncio.gather(waiter, stdout, stderr, return_exceptions=True)
