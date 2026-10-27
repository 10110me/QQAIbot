"""Consistent DB backup. Credentials stay in subprocess environment, not arguments."""
import argparse
import os
import shutil
import sqlite3
import subprocess
from datetime import datetime, UTC
from pathlib import Path
from urllib.parse import unquote, urlsplit

from dotenv import load_dotenv


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="./backups")
    parser.add_argument("--include-local-objects", action="store_true")
    parser.add_argument("--include-workspaces", action="store_true")
    args = parser.parse_args()
    load_dotenv()
    url = os.environ.get("OSA_DATABASE_URL", "sqlite+aiosqlite:///./osa.db")
    target = Path(args.output)/datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target.mkdir(parents=True, exist_ok=False)
    target.chmod(0o700)
    if url.startswith("sqlite+aiosqlite:///"):
        source = Path(url.split("///",1)[1]).resolve()
        if not source.exists():
            raise SystemExit("SQLite database does not exist")
        with sqlite3.connect(source) as original, sqlite3.connect(target/"osa.db") as copy:
            original.backup(copy)
    elif url.startswith("postgresql"):
        parsed = urlsplit(url.replace("postgresql+asyncpg", "postgresql", 1))
        env = {**os.environ, "PGHOST": parsed.hostname or "localhost", "PGPORT": str(parsed.port or 5432),
               "PGUSER": unquote(parsed.username or ""), "PGPASSWORD": unquote(parsed.password or ""),
               "PGDATABASE": unquote(parsed.path.lstrip("/"))}
        subprocess.run(["pg_dump", "--format=custom", "--file", str(target/"database.dump")], env=env, check=True)
    else:
        raise SystemExit("Only persistent SQLite/PostgreSQL can be backed up")
    if args.include_local_objects:
        root = Path(os.environ.get("OSA_DATA_DIR", "./data"))/"objects"
        if root.exists():
            shutil.copytree(root, target/"objects", symlinks=True)
    registry_url = os.environ.get("OSA_V2_DATABASE_URL", "")
    if registry_url and not registry_url.startswith(("postgresql://", "postgres://")) and Path(registry_url).is_file():
        with sqlite3.connect(registry_url) as original, sqlite3.connect(target/"registry.db") as copy:
            original.backup(copy)
    if args.include_workspaces:
        workspaces = Path(os.environ.get("OSA_WORKSPACE_DIR", "./data/workspaces"))
        if workspaces.exists():
            shutil.copytree(workspaces, target/"workspaces", symlinks=True)
    print("Backup created at", target)


if __name__ == "__main__":
    main()
