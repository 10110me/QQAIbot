"""Read-only deployment checks; stdlib only, never changes the host."""
import json
import platform
import shutil
import subprocess
from pathlib import Path


def main() -> int:
    if platform.system() != "Linux":
        print("低内存配置需要 Linux Docker 主机。")
        return 1
    memory = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, value = line.split(":", 1)
        memory[key] = int(value.strip().split()[0])
    ram = memory["MemTotal"] / 1024**2
    swap = memory["SwapTotal"] / 1024**2
    disk = shutil.disk_usage(Path(__file__).resolve().parents[1]).free / 1024**3
    print(f"RAM {ram:.2f} GiB; Swap {swap:.2f} GiB; 项目分区可用 {disk:.1f} GiB")
    errors = []
    if ram < 0.8:
        errors.append("实际内存不足 0.8 GiB。")
    if swap < 3.5:
        errors.append("请先配置至少 4 GiB Swap，再启动浏览器。")
    if disk < 12:
        errors.append("可用磁盘不足 12 GiB；浏览器镜像和构建需要空间，建议至少 20 GiB。")
    if platform.machine() not in {"x86_64", "AMD64"}:
        errors.append("尚未验证此 CPU 架构的 Playwright 镜像，请先确认镜像架构支持。")
    try:
        result = subprocess.run(["docker", "info", "--format", "{{json .}}"],
                                capture_output=True, text=True, timeout=15, check=True)
        info = json.loads(result.stdout)
        if not info.get("MemoryLimit") or not info.get("SwapLimit"):
            errors.append("Docker 未报告内存/Swap 限额支持，不能按此配置控制资源。")
        subprocess.run(["docker", "compose", "version"], check=True, timeout=10,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError, ValueError):
        errors.append("无法读取 Docker 状态或 Compose v2 不可用；请检查安装和 socket 权限。")
    for error in errors:
        print("需处理：", error)
    if not errors:
        print("前置检查通过。仍需实际 QQ/DeepSeek 联调，不能据此保证内存峰值。")
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
