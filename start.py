"""Stdlib-only Docker launcher. Select normal/lowmem in startup.ini."""
import argparse
import configparser
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent

NORMAL = {
    "OPENCODE_MEMORY": "768m", "OPENCODE_SWAP": "1536m",
    "CODEX_MEMORY": "1536m", "CODEX_SWAP": "2560m",
    "THINK_STEPS": "12", "THINK_SECONDS": "180",
    "DB_POOL": "5", "WORKERS": "10", "TASKS": "3", "CONTEXT": "20000",
    "DOWNLOAD": "20000000", "BROWSER_LAZY": "false", "BROWSER_IDLE": "60",
    "HEAVY_SLOT": "", "PARSER_MB": "768", "RUNNER_MEMORY": "256m", "RUNNER_SWAP": "512m",
    "APP_MEMORY": "1536m", "APP_SWAP": "2304m", "APP_CPUS": "2", "APP_PIDS": "128",
    "BROWSER_MEMORY": "2g", "BROWSER_SWAP": "4g", "BROWSER_CPUS": "2", "BROWSER_PIDS": "256",
    "BROWSER_SHM": "512m", "INDEXER_MEMORY": "1g", "INDEXER_SWAP": "2g",
    "INDEXER_CPUS": "1", "INDEXER_PIDS": "64", "INDEXER_TMPFS": "256m",
    "SCREENSHOT_MEMORY": "1g", "SCREENSHOT_SWAP": "2g", "SCREENSHOT_CPUS": "1",
    "SCREENSHOT_PIDS": "192", "SCREENSHOT_SHM": "256m", "SCREENSHOT_TMPFS": "256m",
    "SANDBOX_MEMORY": "192m", "SANDBOX_SWAP": "512m", "SANDBOX_PIDS": "64",
}
LOWMEM = {
    "OPENCODE_MEMORY": "384m", "OPENCODE_SWAP": "1536m",
    "CODEX_MEMORY": "512m", "CODEX_SWAP": "1536m",
    "THINK_STEPS": "6", "THINK_SECONDS": "45",
    "DB_POOL": "1", "WORKERS": "1", "TASKS": "1", "CONTEXT": "10000",
    "DOWNLOAD": "5000000", "BROWSER_LAZY": "true", "BROWSER_IDLE": "30",
    "HEAVY_SLOT": "/slots/heavy.lock", "PARSER_MB": "256",
    "RUNNER_MEMORY": "128m", "RUNNER_SWAP": "384m",
    "APP_MEMORY": "192m", "APP_SWAP": "640m", "APP_CPUS": "0.8", "APP_PIDS": "64",
    "BROWSER_MEMORY": "448m", "BROWSER_SWAP": "1792m", "BROWSER_CPUS": "0.8",
    "BROWSER_PIDS": "192", "BROWSER_SHM": "64m", "INDEXER_MEMORY": "160m",
    "INDEXER_SWAP": "512m", "INDEXER_CPUS": "0.7", "INDEXER_PIDS": "48", "INDEXER_TMPFS": "48m",
    "SCREENSHOT_MEMORY": "256m", "SCREENSHOT_SWAP": "1024m", "SCREENSHOT_CPUS": "0.8",
    "SCREENSHOT_PIDS": "128", "SCREENSHOT_SHM": "64m", "SCREENSHOT_TMPFS": "48m",
    "SANDBOX_MEMORY": "96m", "SANDBOX_SWAP": "256m", "SANDBOX_PIDS": "48",
}


@dataclass(frozen=True)
class Startup:
    mode: str
    sandbox: bool
    project: str
    storage: str = "sqlite"
    work: bool = False
    coding: str = "builtin"


def load_config(path: Path) -> Startup:
    parser = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=("#", ";"))
    with path.open(encoding="utf-8") as stream:
        parser.read_file(stream)
    if set(parser.sections()) != {"runtime"} or parser.defaults():
        raise ValueError("启动配置只允许 [runtime]，不使用 DEFAULT 或其他配置节")
    section = parser["runtime"]
    if not {"mode", "sandbox", "project"} <= set(section) or set(section) - {"mode", "sandbox", "project", "storage", "work", "coding"}:
        raise ValueError("[runtime] 必须包含 mode、sandbox、project；请检查拼写")
    mode = section["mode"].strip().lower()
    if mode not in {"normal", "lowmem"}:
        raise ValueError("mode 只能是 normal（普通）或 lowmem（低内存）")
    project = section["project"].strip()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", project):
        raise ValueError("project 必须是小写字母/数字开头的 1～63 位小写名称，可含 - 和 _")
    storage = section.get("storage", "sqlite").strip().lower()
    if storage not in {"sqlite", "postgres"}:raise ValueError("storage 只能是 sqlite 或 postgres；切换存储前必须迁移数据")
    work = section.getboolean("work", fallback=False)
    if work and not section.getboolean("sandbox"):raise ValueError("启用 work 必须同时启用 sandbox")
    coding = section.get("coding", "builtin").strip().lower()
    if coding not in {"builtin", "opencode", "codex"}:
        raise ValueError("coding 只能为 builtin、opencode 或 codex")
    if coding in {"opencode", "codex"} and not work:
        raise ValueError("coding=opencode/codex 必须启用 work=true")
    return Startup(mode, section.getboolean("sandbox"), project, storage, work, coding)


def environment(config: Startup, base: dict[str, str] | None = None, root: Path = ROOT) -> dict[str, str]:
    # Prevent stale launcher values and inherited Compose profiles overriding the INI.
    result = {key: value for key, value in (os.environ if base is None else base).items()
              if not key.startswith("OSA_PROFILE_") and key != "COMPOSE_PROFILES"}
    values = {**NORMAL, **(LOWMEM if config.mode == "lowmem" else {})}
    values.update(MODE=config.mode, SANDBOX=str(config.sandbox).lower(), WORK=str(config.work).lower(), CODING=config.coding)
    if config.storage == "postgres":
        password = result.get("OSA_DB_PASSWORD", "")
        if not password:
            for line in (root / ".env").read_text().splitlines():
                if line.strip().startswith("OSA_DB_PASSWORD="):
                    password = line.strip().split("=", 1)[1].strip().strip('"\'')
        if not re.fullmatch(r"[A-Za-z0-9_-]{24,128}", password):
            raise ValueError("PostgreSQL 需要 .env 中 24～128 位 URL-safe 随机 OSA_DB_PASSWORD，请使用 init_env.py 生成或 secrets.token_hex(32)")
        result["OSA_DB_PASSWORD"] = password
        values["DATABASE_URL"] = "postgresql+asyncpg://osa:"+password+"@postgres:5432/osa"
        values["V2_DATABASE_URL"] = "postgresql://osa:"+password+"@postgres:5432/osa"
    else:
        values["DATABASE_URL"] = "sqlite+aiosqlite:////app/data/osa.db"
        values["V2_DATABASE_URL"] = "/app/control/registry.db"
    result.update({"OSA_PROFILE_" + key: value for key, value in values.items()})
    return result


def compose(config: Startup, root: Path = ROOT, all_profiles: bool = False) -> list[str]:
    command = ["docker", "compose", "--parallel", "1", "--project-directory", str(root),
               "--env-file", str(root / ".env"), "-p", config.project,
               "-f", str(root / "docker-compose.runtime.yml")]
    if config.sandbox or all_profiles:
        command += ["--profile", "sandbox"]
    if config.storage == "postgres" or all_profiles:command += ["--profile", "postgres"]
    if all_profiles:command += ["--profile", "backup"]
    if config.coding == "opencode" or all_profiles:command += ["--profile", "opencode"]
    if config.coding == "codex" or all_profiles:command += ["--profile", "codex"]
    return command


def plan(action: str, config: Startup, no_build: bool = False, root: Path = ROOT) -> list[list[str]]:
    command = compose(config, root)
    if action == "check":
        return [command + ["config", "--quiet"]]
    if action == "backup":
        all_command=compose(config,root,all_profiles=True)
        operations=[command+["config","--quiet"],all_command+["stop","app","work-worker","browser-worker"]]
        if config.coding == "opencode":
            operations += [command+["up","-d","--no-deps","opencode-gateway"],
                command+["exec","-T","opencode-gateway","python","/app/service.py","--backup"]]
        if config.coding == "codex":
            operations += [command+["up","-d","--no-deps","codex-gateway"],
                command+["exec","-T","codex-gateway","python","/app/service.py","--backup"]]
        operations += [all_command+["stop","opencode-gateway","codex-gateway"]]
        if config.storage=="postgres":operations += [all_command+["up","-d","--wait","postgres"]]
        return operations+[command+["run","--rm","--no-deps","app","python","/app/scripts/backup.py",
            "--output","/app/data/backups","--include-local-objects","--include-workspaces"],
            all_command+["run","--rm","--no-deps","backup-browser"],command+["up","-d"]]
    if action in {"down", "status", "logs"}:
        arguments = {"down": ["down"], "status": ["ps", "--all"], "logs": ["logs", "--tail=100"]}
        return [compose(config, root, all_profiles=True) + arguments[action]]
    commands = [command + ["config", "--quiet"]]
    if config.mode == "lowmem":
        commands += [[sys.executable, str(root / "scripts/check_lowmem.py")]]
    if action == "build" or not no_build:
        commands += [command + ["build"]]
        if config.sandbox:
            commands += [["docker", "build", "-f", str(root / "workers/sandbox/Runner.Dockerfile"),
                          "-t", "osa-python-runner:0.2", str(root)]]
        if config.coding == "opencode":
            commands += [["docker", "build", "-f", str(root / "workers/opencode/Runner.Dockerfile"),
                          "-t", "osa-opencode-runner:1.2.27", str(root)]]
        if config.coding == "codex":
            commands += [["docker", "build", "-f", str(root / "workers/codex/Runner.Dockerfile"),
                          "-t", "osa-codex-runner:0.147.0", str(root)]]
    if action == "up":
        if config.storage == "postgres":
            commands += [compose(config, root, all_profiles=True) + ["up", "-d", "--wait", "postgres"]]
        if not config.sandbox:
            commands += [compose(config, root, all_profiles=True) + ["stop", "sandbox-worker"]]
        if config.coding != "opencode":
            commands += [compose(config, root, all_profiles=True) + ["stop", "opencode-gateway"]]
        if config.coding != "codex":
            commands += [compose(config, root, all_profiles=True) + ["stop", "codex-gateway"]]
        commands += [command + ["up", "-d", "--remove-orphans"]]
    return commands


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="在 startup.ini 选择 normal / lowmem，统一管理启动。")
    parser.add_argument("action", choices=["up", "build", "check", "down", "status", "logs", "backup"],
                        nargs="?", default="up")
    parser.add_argument("--config", type=Path, default=ROOT / "startup.ini")
    parser.add_argument("--no-build", action="store_true", help="up 时使用已经构建的镜像")
    args = parser.parse_args(argv)
    try:
        if args.no_build and args.action != "up":
            raise ValueError("--no-build 仅可用于 up")
        config = load_config(args.config)
        if not (ROOT / ".env").is_file():
            raise ValueError("缺少 .env；请先执行 python3 scripts/init_env.py 并填写 QQ 与模型配置")
        label = "低内存" if config.mode == "lowmem" else "普通"
        print(f"模式：{label}（{config.mode}）；隔离执行：{'开' if config.sandbox else '关'}；项目：{config.project}；存储：{config.storage}；工作：{config.work}", flush=True)
        for command in plan(args.action, config, args.no_build):
            # Argument arrays avoid shell evaluation; credentials remain in .env/environment.
            subprocess.run(command, cwd=ROOT, env=environment(config), check=True)
        print("操作完成。切换模式请修改 startup.ini，再执行 python3 start.py up。")
        return 0
    except (OSError, ValueError, configparser.Error) as exc:
        print("启动配置或执行环境错误：" + str(exc), file=sys.stderr)
        return 2
    except subprocess.CalledProcessError as exc:
        print("命令未成功，已停止后续步骤；未删除数据卷。", file=sys.stderr)
        return exc.returncode if exc.returncode > 0 else 1
    except KeyboardInterrupt:
        print("已中断；未删除数据卷。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
