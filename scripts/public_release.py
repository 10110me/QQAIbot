"""Export an allowlisted source tree, refusing known private values and credentials."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
from pathlib import Path
from urllib.parse import unquote, urlsplit


ROOT_FILES = frozenset({
    ".dockerignore", ".env.example", ".gitattributes", ".gitignore", "README.md",
    "CONTRIBUTING.md", "SECURITY.md", "THIRD_PARTY_NOTICES.md", "LICENSE",
    "LICENSE.md", "COPYING", "Dockerfile", "alembic.ini", "pyproject.toml",
    "requirements.txt", "requirements-dev.txt", "start.py", "startup.ini",
    "docker-compose.yml", "docker-compose.full.yml", "docker-compose.lowmem.yml",
    "docker-compose.runtime.yml", "安装说明.txt", "1核1G部署说明.txt", "4核8G部署说明.txt",
})
SOURCE_TREES = ("src/osa", "tests", "workers", "migrations", "examples", "deploy", ".github/workflows")
SCRIPT_FILES = frozenset({
    "apply_nova_persona.py", "apply_nova_v4.py", "backup.py", "check_lowmem.py",
    "import_maibot.py", "init_env.py", "package.py", "preview_webui.py",
    "prompt_benchmark.py", "public_release.py", "setup_swap.sh",
})
DOC_FILES = frozenset({
    "architecture.md", "coding-provider.md", "deepseek-browser.md", "deployment.md",
    "design-v2.md", "design-v3.md",
    "full-features.md", "low-memory.md", "migration-v3.md", "model-routing.md",
    "plugin-api.md", "privacy.md", "server-4c8g.md", "startup-modes.md",
    "v2-implementation.md", "v3-implementation.md", "ai-workspace.md",
    "model-bulk-replacement.md", "model-retry.md",
    "nova-persona-card.md", "nova-v4-user-prompt.txt", "public-release.md",
})
SOURCE_SUFFIXES = frozenset({".py", ".js", ".css", ".html", ".yml", ".yaml", ".conf", ".Dockerfile"})
BLOCKED_PARTS = frozenset({
    ".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache",
    "data", "private", "logs", "backups", "exports", "tmp", "temp", "browserdata",
    "browser-profile", "workspaces", "sandbox-jobs", ".codex-remote-attachments",
})
CREDENTIAL_PATTERNS = (
    ("private-key", re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----")),
    ("github-token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b")),
    ("api-key", re.compile(r"\bsk-(?:proj-|ant-api\d+-)?[A-Za-z0-9_-]{24,}\b")),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("session-jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\b")),
    ("personal-qq-email", re.compile(r"\b[1-9][0-9]{4,11}@qq\.com\b", re.I)),
    ("windows-user-path", re.compile(r"[A-Za-z]:[\\/]Users[\\/](?!Public\b|example\b|user\b|USERNAME\b)[^\s\\/\"']+", re.I)),
    ("nas-volume-path", re.compile(r"/vol[0-9]+/[0-9]+/")),
)


def source_files(root: Path) -> list[Path]:
    root = root.resolve()
    candidates = [root / name for name in ROOT_FILES]
    candidates += [root / "scripts" / name for name in SCRIPT_FILES]
    candidates += [root / "docs" / name for name in DOC_FILES]
    for tree in SOURCE_TREES:
        directory = root / tree
        if directory.is_symlink() or directory.is_junction():
            raise ValueError(f"Linked source directory: {tree}")
        if not directory.exists():
            continue
        for parent, directories, files in directory.walk(follow_symlinks=False):
            for name in list(directories):
                child = parent / name
                if name in BLOCKED_PARTS:
                    directories.remove(name)
                elif child.is_symlink() or child.is_junction():
                    raise ValueError(f"Linked source directory: {child.relative_to(root)}")
            for name in files:
                path = parent / name
                if path.suffix in SOURCE_SUFFIXES or name == "Dockerfile":
                    candidates.append(path)
    selected = []
    for path in candidates:
        if not path.exists() and not path.is_symlink():
            continue
        relative = path.relative_to(root)
        if any(part in BLOCKED_PARTS for part in relative.parts):
            raise ValueError(f"Forbidden source path: {relative}")
        if path.is_symlink() or path.resolve() != root / relative or not path.is_file():
            raise ValueError(f"Unsafe source path: {relative}")
        selected.append(path)
    return sorted(set(selected))


def private_values(root: Path) -> set[str]:
    values: set[str] = set()
    environment = {}
    for path in root.glob(".env*"):
        if path.name == ".env.example" or not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            environment[key.strip().removeprefix("export ")] = value.strip().strip("\"'")
    for key, value in environment.items():
        if re.search(r"TOKEN|PASSWORD|SECRET|API_KEY", key) and len(value) >= 12:
            if not value.startswith(("replace-", "your-")):
                values.add(value)
        if key in {"OSA_BOT_ID", "OSA_ALLOWED_GROUPS", "OSA_QQ_ADMIN_IDS"}:
            values.update(re.findall(r"(?<!\d)[1-9]\d{5,11}(?!\d)", value))
        if "URL" in key:
            parsed = urlsplit(value)
            if parsed.password and len(parsed.password) >= 8:
                values.add(unquote(parsed.password))
    directory = root / "data" / "private"
    encrypted = directory / "runtime-config.enc"
    if encrypted.exists():
        from cryptography.fernet import Fernet

        document = json.loads(Fernet((directory / "runtime-config.key").read_bytes().strip()).decrypt(encrypted.read_bytes()))
        for provider in document.get("providers", []):
            if provider.get("api_key"):
                values.add(provider["api_key"])
            for key in ("default_headers", "default_query"):
                values.update(value for value in provider.get(key, {}).values() if len(value) >= 12)
        values.update(document.get("persona", {}).get("owner_qq_ids", []))
    private_list = root / ".public-release-private.json"
    if private_list.exists():
        extra = json.loads(private_list.read_text(encoding="utf-8"))
        if not isinstance(extra, list) or not all(isinstance(value, str) and value for value in extra):
            raise ValueError("Invalid private publication denylist")
        values.update(extra)
    return {value for value in values if value}


def findings(text: str, secrets: set[str]) -> list[str]:
    reasons = [name for name, pattern in CREDENTIAL_PATTERNS if pattern.search(text)]
    if any(value in text for value in secrets):
        reasons.append("known-private-value")
    return reasons


def export_source(root: Path, output: Path) -> dict:
    root = root.resolve()
    output = output.resolve()
    if output == root or root in output.parents and output.relative_to(root).parts[0] not in {"dist", "tmp"}:
        raise ValueError("Use a new directory under dist/ or tmp/, or outside the source tree")
    if output.exists():
        raise ValueError("Output already exists; use a new directory")
    secrets = private_values(root)
    paths = source_files(root)
    violations = []
    content = {}
    for path in paths:
        relative = path.relative_to(root).as_posix()
        raw = path.read_bytes()
        text = raw.decode("utf-8-sig")
        for reason in findings(text, secrets):
            violations.append({"path": relative, "rule": reason})
        content[relative] = text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
    if violations:
        raise ValueError(json.dumps({"publication_blocked": violations}, ensure_ascii=False))
    output.mkdir(parents=True)
    manifest = {"project": "TideTalk", "version": 1, "files": {}}
    for relative, raw in content.items():
        target = output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        shutil.copymode(root / relative, target)
        manifest["files"][relative] = hashlib.sha256(raw).hexdigest()
    (output / "public-source-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"project": "TideTalk", "files": len(content), "credential_findings": 0, "output": str(output)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("dist/tidetalk"))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    print(json.dumps(export_source(root, args.output), ensure_ascii=False))


if __name__ == "__main__":
    main()
