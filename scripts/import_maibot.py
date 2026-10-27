"""Import a MaiBot installation without exporting its private data off-host.

This command is intended to run in a one-off QQAIBOT container with the old
MaiBot tree mounted read-only.  It imports model/persona configuration into the
encrypted runtime document, creates a private SQLite archive of the old memory
tables, and activates only memories whose owner can be identified exactly.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import re
import shutil
import sqlite3
import time
import tomllib
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

from osa.infrastructure.runtime_config import (
    TASK_NAMES,
    ModelConfig,
    OutputFilterConfig,
    PersonaProfile,
    PromptTemplate,
    ProviderConfig,
    RuntimeConfiguration,
    RuntimeConfigurationStore,
    TaskConfig,
)

MAIN_MEMORY_TABLES = (
    "person_info",
    "chat_history",
    "chat_sessions",
    "expressions",
    "jargons",
    "__legacy_v1_person_info",
    "__legacy_v1_chat_history",
    "__legacy_v1_chat_streams",
    "__legacy_v1_expression",
    "__legacy_v1_jargon",
)
OWNER_KEYS = {"user_id", "person_id", "owner_id", "qq", "qq_id", "actor_id"}


def _read_toml(path: Path) -> dict[str, Any]:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def _identifier(prefix: str, value: str, index: int) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:10]
    return f"{prefix}{index:02d}_{digest}"


def _string_mapping(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(key): str(item) for key, item in value.items()}


def _model_configuration(
    model_doc: dict[str, Any], current: RuntimeConfiguration
) -> tuple[list[ProviderConfig], list[ModelConfig], dict[str, TaskConfig]]:
    providers: list[ProviderConfig] = []
    provider_lookup: dict[str, str] = {}
    for index, raw in enumerate(model_doc.get("api_providers", []), 1):
        name = str(raw.get("name") or f"MaiBot Provider {index}")
        identifier = _identifier("maibot_", name, index)
        provider_lookup[name] = identifier
        providers.append(
            ProviderConfig(
                id=identifier,
                name=name,
                base_url=str(raw.get("base_url") or "https://api.openai.com/v1"),
                api_key=str(raw.get("api_key") or ""),
                client_type="openai",
                auth_type=str(raw.get("auth_type") or "bearer"),
                auth_header_name=str(raw.get("auth_header_name") or "Authorization"),
                auth_header_prefix=str(raw.get("auth_header_prefix") or "Bearer "),
                auth_query_name=str(raw.get("auth_query_name") or ""),
                model_list_endpoint=str(raw.get("model_list_endpoint") or "models"),
                max_retry=int(raw.get("max_retry", 2)),
                timeout=float(raw.get("timeout", 30)),
                retry_interval=float(raw.get("retry_interval", 0.8)),
                default_headers=_string_mapping(raw.get("default_headers")),
                default_query=_string_mapping(raw.get("default_query")),
            )
        )
    if not providers:
        raise RuntimeError("MaiBot model_config.toml contains no API providers")

    models: list[ModelConfig] = []
    used_model_ids: set[str] = set()
    for raw in model_doc.get("models", []):
        provider_name = str(raw.get("api_provider") or "")
        provider_id = provider_lookup.get(provider_name)
        if provider_id is None:
            raise RuntimeError("A MaiBot model references an unknown provider")
        extra = raw.get("extra_params") or {}
        if isinstance(extra, str):
            extra = json.loads(extra or "{}")
        identifier = str(raw.get("model_identifier") or "").strip()
        if identifier in used_model_ids:
            identifier = f"{identifier}__{provider_id}"
        used_model_ids.add(identifier)
        models.append(
            ModelConfig(
                model_identifier=identifier,
                name=str(raw.get("name") or identifier),
                api_provider=provider_id,
                price_in=float(raw.get("price_in", 0)),
                price_out=float(raw.get("price_out", 0)),
                cache=bool(raw.get("cache", False)),
                cache_price_in=float(raw.get("cache_price_in", 0)),
                visual=bool(raw.get("visual", False)),
                force_stream_mode=bool(raw.get("force_stream_mode", False)),
                extra_params=extra if isinstance(extra, dict) else {},
            )
        )

    tasks: dict[str, TaskConfig] = {}
    raw_tasks = model_doc.get("model_task_config", {})
    for name in TASK_NAMES:
        raw = raw_tasks.get(name, raw_tasks.get('planner', {}) if name == 'decision' else {})
        tasks[name] = TaskConfig(
            model_list=[str(value) for value in raw.get("model_list", [])],
            max_tokens=int(raw.get("max_tokens", 800)),
            temperature=float(raw.get("temperature", 0.7)),
            slow_threshold=float(raw.get("slow_threshold", 10)),
            selection_strategy=str(raw.get("selection_strategy") or "sequential"),
            hard_timeout=min(180.0, max(1.0, float(raw.get("hard_timeout", 45)))),
        )
    return providers, models, tasks


def _prompts(
    source: Path, private_import: Path, bot_doc: dict[str, Any]
) -> tuple[list[PromptTemplate], list[str]]:
    default_dir = source / "prompts" / "zh-CN"
    custom_dir = source / "data" / "custom_prompts" / "zh-CN"
    prompt_files: dict[str, Path] = {}
    for directory in (default_dir, custom_dir):
        if directory.exists():
            for path in sorted(directory.glob("*.prompt")):
                prompt_files[path.stem] = path
    result = [
        PromptTemplate(
            name=name,
            content=path.read_text(encoding="utf-8"),
            source="maibot",
            enabled=True,
            compatible_stage="archived",
        )
        for name, path in sorted(prompt_files.items())
    ]

    chat = bot_doc.get("chat", {})
    active: list[str] = []
    for name, key in (
        ("maibot_group_chat_config", "group_chat_prompt"),
        ("maibot_private_chat_config", "private_chat_prompts"),
    ):
        content = str(chat.get(key) or "").strip()
        if content:
            result.append(
                PromptTemplate(
                    name=name,
                    content=content,
                    source="maibot",
                    enabled=True,
                    compatible_stage="archived",
                )
            )

    prompt_archive = private_import / "prompts"
    if prompt_archive.exists():
        shutil.rmtree(prompt_archive)
    prompt_archive.mkdir(parents=True, mode=0o700)
    for directory_name, directory in (
        ("defaults-zh-CN", default_dir),
        ("custom-zh-CN", custom_dir),
    ):
        if directory.exists():
            copied = prompt_archive / directory_name
            shutil.copytree(directory, copied)
            for path in copied.rglob("*"):
                path.chmod(0o700 if path.is_dir() else 0o600)
    return result, active


def _persona(
    bot_doc: dict[str, Any], current: PersonaProfile, active_prompts: list[str]
) -> PersonaProfile:
    bot = bot_doc.get("bot", {})
    personality = bot_doc.get("personality", {})
    name = str(bot.get("nickname") or current.name)
    aliases = [str(value) for value in bot.get("alias_names", []) if str(value).strip()]
    return PersonaProfile(
        id=current.id,
        name=name,
        aliases=aliases,
        owner_address=current.owner_address,
        identity=f"你是{name}。这是由管理员从原 MaiBot 迁入并锁定的稳定身份。",
        personality=str(personality.get("personality") or current.personality),
        reply_style=str(personality.get("reply_style") or current.reply_style),
        relationship_rules=current.relationship_rules,
        active_prompt_names=active_prompts,
        enabled=True,
    )


def import_configuration(source: Path, data_dir: Path) -> dict[str, int]:
    model_path = source / "config" / "model_config.toml"
    bot_path = source / "config" / "bot_config.toml"
    model_doc, bot_doc = _read_toml(model_path), _read_toml(bot_path)
    store = RuntimeConfigurationStore(str(data_dir), RuntimeConfiguration())
    current = store.snapshot()
    private_import = data_dir / "private" / "maibot-import"
    private_import.mkdir(parents=True, exist_ok=True, mode=0o700)

    # Keep the exact source configuration on the NAS for audit and future fields.
    for original in (model_path, bot_path):
        target = private_import / original.name
        shutil.copy2(original, target)
        target.chmod(0o600)

    providers, models, tasks = _model_configuration(model_doc, current)
    prompts, active = _prompts(source, private_import, bot_doc)
    receive = bot_doc.get("message_receive", {})
    output_filter = OutputFilterConfig(
        blocked_words=[str(value) for value in receive.get("ban_words", [])],
        blocked_regex=[str(value) for value in receive.get("ban_msgs_regex", [])],
        replacement=current.output_filter.replacement,
        strip_reasoning=True,
        protect_identity=True,
        max_replacements=current.output_filter.max_replacements,
    )
    imported = RuntimeConfiguration(
        version=current.version,
        revision=current.revision,
        providers=providers,
        models=models,
        task_configs=tasks,
        persona=_persona(bot_doc, current.persona, active),
        prompts=prompts,
        output_filter=output_filter,
    )
    store.replace(imported, expected_revision=current.revision)
    return {
        "providers": len(providers),
        "models": len(models),
        "tasks": len(tasks),
        "prompts": len(prompts),
    }


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _copy_selected_tables(source: Path, destination: Path, tables: Iterable[str]) -> dict[str, int]:
    temporary = destination.with_suffix(".tmp")
    temporary.unlink(missing_ok=True)
    source_uri = "file:" + source.resolve().as_posix() + "?mode=ro"
    connection = sqlite3.connect(source_uri, uri=True)
    connection.execute("ATTACH DATABASE ? AS imported", (str(temporary),))
    available = {
        row[0]
        for row in connection.execute("SELECT name FROM main.sqlite_master WHERE type='table'")
    }
    counts: dict[str, int] = {}
    try:
        connection.execute("BEGIN")
        for table in tables:
            if table not in available:
                continue
            quoted = _quote(table)
            connection.execute(f"CREATE TABLE imported.{quoted} AS SELECT * FROM main.{quoted}")
            counts[table] = int(
                connection.execute(f"SELECT count(*) FROM imported.{quoted}").fetchone()[0]
            )
        connection.commit()
    finally:
        connection.close()
    os.replace(temporary, destination)
    destination.chmod(0o600)
    return counts


def _backup_database(source: Path, destination: Path) -> None:
    temporary = destination.with_suffix(".tmp")
    temporary.unlink(missing_ok=True)
    old = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    new = sqlite3.connect(temporary)
    try:
        old.backup(new, pages=4096)
    finally:
        new.close()
        old.close()
    os.replace(temporary, destination)
    destination.chmod(0o600)


def archive_memories(source: Path, data_dir: Path) -> tuple[Path, dict[str, int]]:
    destination = data_dir / "private" / "maibot-import"
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    main_source = source / "data" / "MaiBot.db"
    memorix_source = source / "data" / "a-memorix" / "metadata" / "metadata.db"
    main_counts = _copy_selected_tables(
        main_source, destination / "maibot-memory-archive.db", MAIN_MEMORY_TABLES
    )
    _backup_database(memorix_source, destination / "a-memorix-archive.db")
    return destination, main_counts


def _decode_json(raw: Any) -> Any:
    if not isinstance(raw, str):
        return raw
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return raw


def _memory_texts(raw: Any) -> list[str]:
    value = _decode_json(raw)
    result: list[str] = []
    if isinstance(value, str):
        text = value.strip()
        if text:
            result.append(text)
    elif isinstance(value, list):
        for item in value:
            result.extend(_memory_texts(item))
    elif isinstance(value, dict):
        preferred = ("memory", "content", "text", "fact", "description", "value")
        found = False
        for key in preferred:
            if key in value:
                result.extend(_memory_texts(value[key]))
                found = True
        if not found:
            for item in value.values():
                if isinstance(item, (dict, list)):
                    result.extend(_memory_texts(item))
    return list(dict.fromkeys(text[:20000] for text in result if 1 <= len(text) <= 20000))


def _owner(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if re.fullmatch(r"\d{5,20}", text):
        return text
    match = re.fullmatch(r"(?:qq|user|person)[:_-](\d{5,20})", text, re.IGNORECASE)
    return match.group(1) if match else None


def _owner_from_metadata(raw: Any) -> str | None:
    value = _decode_json(raw)
    found: set[str] = set()

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            for key, item in node.items():
                if str(key).lower() in OWNER_KEYS:
                    candidate = _owner(item)
                    if candidate:
                        found.add(candidate)
                elif isinstance(item, (dict, list)):
                    visit(item)
        elif isinstance(node, list):
            for item in node:
                visit(item)

    visit(value)
    return next(iter(found)) if len(found) == 1 else None


def _target_database(data_dir: Path) -> Path:
    preferred = data_dir / "osa.db"
    candidates = [preferred, *sorted(data_dir.glob("*.db"))]
    for path in dict.fromkeys(candidates):
        if not path.exists():
            continue
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
        finally:
            connection.close()
        if {"events", "memories"}.issubset(tables):
            return path
    raise RuntimeError("Could not find the QQAIBOT SQLite database")


def _insert_memory(
    connection: sqlite3.Connection,
    owner: str,
    text: str,
    provenance: str,
    source_key: str,
    timestamp: float | None,
) -> bool:
    digest = hashlib.sha256(f"{provenance}:{source_key}:{owner}:{text}".encode()).hexdigest()
    event_id, memory_id = "mi_evt_" + digest[:48], "mi_mem_" + digest[:48]
    try:
        occurred = float(timestamp or time.time())
    except (TypeError, ValueError):
        try:
            occurred = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00")).timestamp()
        except ValueError:
            occurred = time.time()
    payload = json.dumps(
        {"imported_from": provenance, "source_key": source_key, "text": "", "segments": []},
        ensure_ascii=False,
    )
    before = connection.total_changes
    connection.execute(
        "INSERT OR IGNORE INTO events "
        "(id,dedupe_key,conversation,message_id,actor_id,event_type,payload,status,"
        "occurred_at,received_at,recalled,collect,error) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            event_id,
            digest[:64],
            f"private:{owner}",
            source_key[:100],
            owner,
            "memory.imported",
            payload,
            "done",
            occurred,
            time.time(),
            0,
            0,
            None,
        ),
    )
    connection.execute(
        "INSERT OR IGNORE INTO memories "
        "(id,content,scope,subject_id,origin_group_id,source_event_id,status,expires_at,version) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (memory_id, text, "private_user", owner, None, event_id, "active", None, 1),
    )
    return connection.total_changes > before


def activate_owned_memories(archive_dir: Path, data_dir: Path) -> dict[str, int]:
    target_path = _target_database(data_dir)
    target = sqlite3.connect(target_path, timeout=60)
    counts = {"person_points": 0, "memorix_paragraphs": 0, "person_profiles": 0, "quarantined": 0}
    try:
        target.execute("PRAGMA foreign_keys=ON")
        main = sqlite3.connect(f"file:{archive_dir / 'maibot-memory-archive.db'}?mode=ro", uri=True)
        try:
            for row_id, user_id, person_id, raw, timestamp in main.execute(
                "SELECT id,user_id,person_id,memory_points,last_known_time "
                "FROM person_info WHERE memory_points IS NOT NULL"
            ):
                owner = _owner(user_id) or _owner(person_id)
                texts = _memory_texts(raw)
                if not owner:
                    counts["quarantined"] += len(texts)
                    continue
                for index, text in enumerate(texts):
                    if _insert_memory(
                        target, owner, text, "maibot.person_info", f"{row_id}:{index}", timestamp
                    ):
                        counts["person_points"] += 1
        finally:
            main.close()

        memorix = sqlite3.connect(f"file:{archive_dir / 'a-memorix-archive.db'}?mode=ro", uri=True)
        try:
            owner_by_hash: dict[str, str] = {}
            for paragraph_hash, raw in memorix.execute(
                "SELECT paragraph_hash,metadata_json FROM external_memory_refs"
            ):
                owner = _owner_from_metadata(raw)
                if owner:
                    owner_by_hash[str(paragraph_hash)] = owner
            for paragraph_hash, content, metadata, created_at, is_deleted in memorix.execute(
                "SELECT hash,content,metadata,created_at,is_deleted FROM paragraphs"
            ):
                if is_deleted:
                    continue
                owner = _owner_from_metadata(metadata) or owner_by_hash.get(str(paragraph_hash))
                if not owner:
                    counts["quarantined"] += 1
                    continue
                if _insert_memory(
                    target,
                    owner,
                    str(content),
                    "a-memorix.paragraph",
                    str(paragraph_hash),
                    created_at,
                ):
                    counts["memorix_paragraphs"] += 1

            for person_id, profile_text, updated_at, snapshot_id in memorix.execute(
                "SELECT p.person_id,p.profile_text,p.updated_at,p.snapshot_id "
                "FROM person_profile_snapshots p "
                "JOIN (SELECT person_id,max(profile_version) version "
                "FROM person_profile_snapshots GROUP BY person_id) latest "
                "ON latest.person_id=p.person_id AND latest.version=p.profile_version"
            ):
                owner = _owner(person_id)
                if not owner:
                    counts["quarantined"] += 1
                    continue
                text = str(profile_text or "").strip()
                if text and _insert_memory(
                    target,
                    owner,
                    text[:20000],
                    "a-memorix.person_profile",
                    str(snapshot_id),
                    updated_at,
                ):
                    counts["person_profiles"] += 1
        finally:
            memorix.close()
        target.commit()
    except Exception:
        target.rollback()
        raise
    finally:
        target.close()
    target_path.chmod(0o600)
    return counts


def _safe_sticker_path(source: Path, raw: Any) -> Path | None:
    text = str(raw or "").strip()
    if not text:
        return None
    candidate = Path(text)
    if not candidate.is_absolute():
        candidate = source / candidate
    try:
        resolved = candidate.resolve()
        resolved.relative_to(source.resolve())
    except (OSError, ValueError):
        return None
    return resolved if resolved.is_file() else None


def _sticker_tags(*values: Any) -> list[str]:
    tags: list[str] = []
    for value in values:
        text = str(value or "").strip()
        for item in re.split(r"[,，、;/|\s]+", text):
            item = item.strip()[:40]
            if item and item not in tags:
                tags.append(item)
    return tags[:20]


def import_stickers(source: Path, data_dir: Path) -> dict[str, int]:
    """Copy MaiBot sticker files and their description/usage memory only."""
    target_path = _target_database(data_dir)
    objects = data_dir / "objects"
    objects.mkdir(parents=True, exist_ok=True, mode=0o700)
    source_db = sqlite3.connect(
        "file:" + (source / "data" / "MaiBot.db").resolve().as_posix() + "?mode=ro",
        uri=True,
    )
    target = sqlite3.connect(target_path, timeout=60)
    counts = {"stickers": 0, "descriptions": 0, "missing_files": 0, "duplicates": 0}
    seen: set[str] = set()
    try:
        queries = [
            (
                "images",
                "SELECT id,full_path,description,image_type,query_count,last_used_time,NULL "
                "FROM images WHERE full_path IS NOT NULL AND is_banned=0",
            ),
            (
                "legacy_images",
                "SELECT id,path,description,type,count,timestamp,NULL "
                "FROM __legacy_v1_images WHERE path IS NOT NULL",
            ),
            (
                "legacy_emoji",
                "SELECT id,full_path,description,format,usage_count,last_used_time,emotion "
                "FROM __legacy_v1_emoji WHERE full_path IS NOT NULL AND is_banned=0",
            ),
        ]
        for table, query in queries:
            try:
                rows = source_db.execute(query)
            except sqlite3.OperationalError:
                if table != "images":
                    continue
                # Some installations still have the V1 images table under its
                # original name rather than __legacy_v1_images.
                try:
                    rows = source_db.execute(
                        "SELECT id,path,description,type,count,timestamp,NULL "
                        "FROM images WHERE path IS NOT NULL"
                    )
                except sqlite3.OperationalError:
                    continue
            for row_id, raw_path, description, _image_type, uses, last_used, emotion in rows:
                path = _safe_sticker_path(source, raw_path)
                if path is None:
                    counts["missing_files"] += 1
                    continue
                content = path.read_bytes()
                digest = hashlib.sha256(content).hexdigest()
                if digest in seen:
                    counts["duplicates"] += 1
                    continue
                seen.add(digest)
                object_id = digest[:32]
                object_path = objects / object_id
                if not object_path.exists():
                    temporary = objects / (object_id + ".tmp")
                    temporary.write_bytes(content)
                    os.replace(temporary, object_path)
                    object_path.chmod(0o600)
                mime = mimetypes.guess_type(path.name)[0] or "image/png"
                created = float(last_used or time.time())
                media_payload = json.dumps(
                    {
                        "object_key": object_id,
                        "mime": mime,
                        "type": "image",
                        "size": len(content),
                        "sha256": digest,
                        "imported_from": "maibot",
                    },
                    ensure_ascii=False,
                )
                sticker_payload = json.dumps(
                    {
                        "asset_id": object_id,
                        "tags": _sticker_tags(description, emotion),
                        "sha256": digest,
                        "uses": int(uses or 0),
                        "last_used": created,
                        "description": str(description or "")[:2000],
                    },
                    ensure_ascii=False,
                )
                target.execute(
                    "INSERT OR IGNORE INTO feature_records "
                    "(id,kind,owner_id,group_id,status,dedupe_key,payload,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        object_id,
                        "media",
                        "maibot-import",
                        None,
                        "active",
                        "maibot-media:" + digest,
                        media_payload,
                        created,
                        time.time(),
                    ),
                )
                sticker_id = hashlib.sha256(("sticker:" + digest).encode()).hexdigest()[:32]
                before = target.total_changes
                target.execute(
                    "INSERT OR IGNORE INTO feature_records "
                    "(id,kind,owner_id,group_id,status,dedupe_key,payload,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        sticker_id,
                        "sticker",
                        "maibot-import",
                        None,
                        "active",
                        "sticker:global:maibot:" + digest,
                        sticker_payload,
                        created,
                        time.time(),
                    ),
                )
                if target.total_changes > before:
                    counts["stickers"] += 1
                if description:
                    analysis_id = hashlib.sha256(
                        f"emoji-description:{table}:{row_id}:{digest}".encode()
                    ).hexdigest()[:32]
                    analysis_payload = json.dumps(
                        {
                            "asset_id": object_id,
                            "text": str(description)[:4000],
                            "emotion": str(emotion or "")[:200],
                            "imported_from": "maibot.emoji_memory",
                        },
                        ensure_ascii=False,
                    )
                    before = target.total_changes
                    target.execute(
                        "INSERT OR IGNORE INTO feature_records "
                        "(id,kind,owner_id,group_id,status,dedupe_key,payload,"
                        "created_at,updated_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?)",
                        (
                            analysis_id,
                            "media_analysis",
                            "maibot-import",
                            None,
                            "succeeded",
                            "maibot-emoji-memory:" + digest,
                            analysis_payload,
                            created,
                            time.time(),
                        ),
                    )
                    if target.total_changes > before:
                        counts["descriptions"] += 1
        target.commit()
    except Exception:
        target.rollback()
        raise
    finally:
        source_db.close()
        target.close()
    return counts


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--skip-stickers", action="store_true")
    args = parser.parse_args()
    source, data_dir = args.source.resolve(), args.data_dir.resolve()
    configuration = import_configuration(source, data_dir)
    result: dict[str, Any] = {"configuration": configuration}
    if not args.skip_stickers:
        result["stickers"] = import_stickers(source, data_dir)
    # Counts and paths only. Never print keys, prompts, person IDs, or memory text.
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
