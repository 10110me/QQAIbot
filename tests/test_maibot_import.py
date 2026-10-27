from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

from osa.infrastructure.runtime_config import RuntimeConfiguration, RuntimeConfigurationStore

SPEC = importlib.util.spec_from_file_location(
    "import_maibot", Path(__file__).parents[1] / "scripts" / "import_maibot.py"
)
assert SPEC and SPEC.loader
IMPORTER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(IMPORTER)


def _target_database(path: Path) -> None:
    db = sqlite3.connect(path)
    db.executescript("""
    CREATE TABLE events (
      id TEXT PRIMARY KEY, dedupe_key TEXT UNIQUE, conversation TEXT, message_id TEXT,
      actor_id TEXT, event_type TEXT, payload JSON, status TEXT, occurred_at REAL,
      received_at REAL, recalled BOOLEAN, collect BOOLEAN, error TEXT
    );
    CREATE TABLE memories (
      id TEXT PRIMARY KEY, content TEXT, scope TEXT, subject_id TEXT,
      origin_group_id TEXT, source_event_id TEXT REFERENCES events(id), status TEXT,
      expires_at REAL, version INTEGER
    );
    CREATE TABLE feature_records (
      id TEXT PRIMARY KEY,kind TEXT,owner_id TEXT,group_id TEXT,status TEXT,
      dedupe_key TEXT,payload JSON,created_at REAL,updated_at REAL,
      UNIQUE(kind,dedupe_key)
    );
    """)
    db.close()


def _source_installation(root: Path) -> None:
    (root / "config").mkdir(parents=True)
    (root / "prompts" / "zh-CN").mkdir(parents=True)
    (root / "data" / "custom_prompts" / "zh-CN").mkdir(parents=True)
    (root / "data" / "a-memorix" / "metadata").mkdir(parents=True)
    tasks = "\n".join(
        f'[model_task_config.{name}]\nmodel_list=["chat-1"]\nmax_tokens=800\ntemperature=0.7\nslow_threshold=10\nselection_strategy="sequential"\nhard_timeout=45'
        for name in IMPORTER.TASK_NAMES
    )
    (root / "config" / "model_config.toml").write_text(
        f"""
[[api_providers]]
name="Provider One"
base_url="https://example.invalid/v1"
api_key="test-private-key"
auth_type="bearer"

[[models]]
model_identifier="chat-1"
name="Chat One"
api_provider="Provider One"

{tasks}
""",
        encoding="utf-8",
    )
    (root / "config" / "bot_config.toml").write_text(
        """
[bot]
nickname="小麦"
alias_names=["麦麦"]
[personality]
personality="温和"
reply_style="简短"
[chat]
group_chat_prompt="群聊旧提示"
private_chat_prompts="私聊旧提示"
[message_receive]
ban_words=["blocked"]
ban_msgs_regex=["secret\\\\d+"]
""",
        encoding="utf-8",
    )
    (root / "prompts" / "zh-CN" / "reply.prompt").write_text("default", encoding="utf-8")
    (root / "data" / "custom_prompts" / "zh-CN" / "reply.prompt").write_text(
        "custom", encoding="utf-8"
    )

    main = sqlite3.connect(root / "data" / "MaiBot.db")
    main.execute(
        "CREATE TABLE person_info "
        "(id INTEGER,user_id TEXT,person_id TEXT,memory_points TEXT,last_known_time TEXT)"
    )
    main.execute(
        "INSERT INTO person_info VALUES "
        "(1,'123456','p','[\"likes tea\"]','2025-01-02T03:04:05+00:00')"
    )
    sticker = root / "data" / "sticker.png"
    sticker.write_bytes(
        bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
            "0000000d49444154789c6360f8cfc00000040101005f70a7780000000049454e44ae426082"
        )
    )
    main.execute(
        "CREATE TABLE images (id INTEGER,image_id TEXT,emoji_hash TEXT,description TEXT,"
        "path TEXT,count INTEGER,timestamp REAL,type TEXT,vlm_processed INTEGER)"
    )
    main.execute(
        "INSERT INTO images VALUES (1,'i','h','happy smile',?,7,123,'emoji',1)",
        (str(sticker.relative_to(root)),),
    )
    main.execute(
        "CREATE TABLE __legacy_v1_emoji (id INTEGER,full_path TEXT,format TEXT,"
        "emoji_hash TEXT,description TEXT,query_count INTEGER,is_registered INTEGER,"
        "is_banned INTEGER,emotion TEXT,record_time REAL,register_time REAL,"
        "usage_count INTEGER,last_used_time REAL)"
    )
    main.commit()
    main.close()

    memory = sqlite3.connect(root / "data" / "a-memorix" / "metadata" / "metadata.db")
    memory.executescript(
        """
        CREATE TABLE external_memory_refs (
          external_id TEXT,paragraph_hash TEXT,source_type TEXT,
          created_at REAL,metadata_json TEXT
        );
        CREATE TABLE paragraphs (
          hash TEXT,content TEXT,metadata TEXT,created_at REAL,is_deleted INTEGER
        );
        CREATE TABLE person_profile_snapshots (
          snapshot_id INTEGER,person_id TEXT,profile_version INTEGER,
          profile_text TEXT,updated_at REAL
        );
        """
    )
    memory.execute(
        "INSERT INTO external_memory_refs VALUES ('x','p1','test',1,?)",
        (json.dumps({"user_id": "123456"}),),
    )
    memory.execute("INSERT INTO paragraphs VALUES ('p1','owned paragraph','{}',1,0)")
    memory.execute("INSERT INTO paragraphs VALUES ('p2','unknown paragraph','{}',1,0)")
    memory.execute("INSERT INTO person_profile_snapshots VALUES (1,'123456',1,'old profile',1)")
    memory.execute("INSERT INTO person_profile_snapshots VALUES (2,'123456',2,'latest profile',2)")
    memory.commit()
    memory.close()


def test_maibot_import_encrypts_configuration_and_imports_stickers(
    tmp_path: Path,
) -> None:
    source, data = tmp_path / "MaiBot", tmp_path / "data"
    data.mkdir()
    _source_installation(source)
    _target_database(data / "osa.db")
    RuntimeConfigurationStore(str(data), RuntimeConfiguration())

    counts = IMPORTER.import_configuration(source, data)
    stickers = IMPORTER.import_stickers(source, data)

    assert counts == {"providers": 1, "models": 1, "tasks": len(IMPORTER.TASK_NAMES), "prompts": 3}
    public = RuntimeConfigurationStore(str(data), RuntimeConfiguration()).public()
    assert public["providers"][0]["has_api_key"] is True
    assert public["providers"][0]["api_key"] == "********"
    assert b"test-private-key" not in (data / "private" / "runtime-config.enc").read_bytes()
    assert public["persona"]["name"] == "小麦"
    assert (
        next(prompt for prompt in public["prompts"] if prompt["name"] == "reply")["content"]
        == "custom"
    )
    assert stickers == {
        "stickers": 1,
        "descriptions": 1,
        "missing_files": 0,
        "duplicates": 0,
    }

    db = sqlite3.connect(data / "osa.db")
    assert db.execute("SELECT count(*) FROM memories").fetchone()[0] == 0
    rows = db.execute("SELECT kind,payload FROM feature_records ORDER BY kind").fetchall()
    db.close()
    assert [row[0] for row in rows] == ["media", "media_analysis", "sticker"]
    assert json.loads(rows[-1][1])["tags"] == ["happy", "smile"]
