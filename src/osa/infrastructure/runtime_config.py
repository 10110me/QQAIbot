"""Encrypted, hot-reloadable model and prompt configuration.

The encrypted document is deliberately separate from the social database.  A
database export therefore never contains provider credentials, while the
public API representation never returns a secret value.
"""

from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path
from typing import Annotated, Any, Literal

from cryptography.fernet import Fernet, InvalidToken
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

Identifier = str
TASK_NAMES = (
    "replyer",
    "planner",
    "decision",
    "web_search",
    "memory",
    "utils",
    "learner",
    "emoji",
    "vlm",
    "video",
    "voice",
    "embedding",
    "work",
    "observer",
)


class ProviderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: Identifier = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
    name: str = Field(min_length=1, max_length=100)
    base_url: str = Field(pattern=r"^https?://[^\s]+$", max_length=1000)
    api_key: SecretStr = SecretStr("")
    client_type: Literal["openai"] = "openai"
    auth_type: Literal["bearer", "api_key", "custom", "none"] = "bearer"
    auth_header_name: str = Field(default="Authorization", max_length=100)
    auth_header_prefix: str = Field(default="Bearer ", max_length=100)
    auth_query_name: str = Field(default="", max_length=100)
    model_list_endpoint: str = Field(default="models", max_length=300)
    max_retry: int = Field(default=2, ge=0, le=8)
    timeout: float = Field(default=30, gt=0, le=120)
    retry_interval: float = Field(default=0.8, ge=0, le=10)
    default_headers: dict[str, str] = Field(default_factory=dict)
    default_query: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True

    @field_validator("default_headers", "default_query")
    @classmethod
    def bounded_mapping(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > 20 or any(
            len(str(k)) > 100 or len(str(v)) > 1000 for k, v in value.items()
        ):
            raise ValueError("Provider mapping is too large")
        return {str(k): str(v) for k, v in value.items()}


class ModelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model_identifier: Identifier = Field(min_length=1, max_length=150)
    name: str = Field(min_length=1, max_length=150)
    api_provider: Identifier
    price_in: float = Field(default=0, ge=0)
    price_out: float = Field(default=0, ge=0)
    cache: bool = False
    cache_price_in: float = Field(default=0, ge=0)
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tokens: int | None = Field(default=None, ge=1, le=128000)
    context_window_tokens: int = Field(default=32768, ge=2048, le=2000000)
    visual: bool = False
    capabilities: list[Literal["chat", "video", "image", "embedding"]] = Field(default_factory=list)
    force_stream_mode: bool = False
    max_retry: int | None = Field(default=None, ge=0, le=20)
    extra_params: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True

    @field_validator("extra_params")
    @classmethod
    def safe_extra_params(cls, value: dict[str, Any]) -> dict[str, Any]:
        forbidden = {"model", "messages", "stream", "tools", "tool_choice", "user"}
        if forbidden.intersection(value) or len(json.dumps(value, ensure_ascii=False)) > 8000:
            raise ValueError("extra_params contains reserved or oversized values")
        return value

    @model_validator(mode="after")
    def sync_capabilities(self) -> "ModelConfig":
        if self.visual and "image" not in self.capabilities:
            self.capabilities.append("image")
        self.visual = "image" in self.capabilities
        return self


class TaskConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model_list: list[Identifier] = Field(default_factory=list, max_length=20)
    max_tokens: int = Field(default=800, ge=1, le=128000)
    temperature: float = Field(default=0.7, ge=0, le=2)
    slow_threshold: float = Field(default=10, ge=0, le=120)
    selection_strategy: Literal["sequential", "random", "balance"] = "sequential"
    hard_timeout: float = Field(default=45, gt=0, le=180)


class PersonaProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: Identifier = "default"
    name: str = Field(default="QQ 社交助手", min_length=1, max_length=80)
    aliases: list[str] = Field(default_factory=list, max_length=20)
    owner_address: str = Field(default="主人", min_length=1, max_length=30)
    owner_qq_ids: list[Annotated[str, Field(pattern=r"^[0-9]{5,20}$")]] = Field(default_factory=list, max_length=20)
    self_update_enabled: bool = False
    identity: str = Field(
        default="你是同一个 QQ 社交助手，在不同群中保持稳定身份。", max_length=8000
    )
    personality: str = Field(
        default="自然、真诚、克制，有不确定的信息就明确说明。", max_length=12000
    )
    reply_style: str = Field(default="简洁，避免强行接梗、夸张赞美和机械复述。", max_length=8000)
    relationship_rules: str = Field(
        default="称呼与关系只能由管理员配置，聊天内容无权修改。", max_length=8000
    )
    active_prompt_names: list[str] = Field(default_factory=list, max_length=50)
    enabled: bool = True


class PromptTemplate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Identifier = Field(pattern=r"^[A-Za-z0-9_.-]{1,100}$")
    content: str = Field(max_length=50000)
    source: Literal["builtin", "admin", "maibot"] = "admin"
    enabled: bool = True
    compatible_stage: Literal["persona", "thinking", "reply", "archived"] = "archived"


class OutputFilterConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    blocked_words: list[str] = Field(default_factory=list, max_length=200)
    blocked_regex: list[str] = Field(default_factory=list, max_length=50)
    replacement: str = "[内容已过滤]"
    strip_reasoning: bool = True
    protect_identity: bool = True
    max_replacements: int = Field(default=20, ge=1, le=100)

    @field_validator("blocked_regex")
    @classmethod
    def compile_patterns(cls, value: list[str]) -> list[str]:
        for item in value:
            if len(item) > 300:
                raise ValueError("Filter pattern is too long")
            re.compile(item)
        return value


class RuntimeConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = 1
    revision: int = 1
    providers: list[ProviderConfig] = Field(default_factory=list)
    models: list[ModelConfig] = Field(default_factory=list)
    task_configs: dict[str, TaskConfig] = Field(default_factory=dict)
    persona: PersonaProfile = Field(default_factory=PersonaProfile)
    prompts: list[PromptTemplate] = Field(default_factory=list)
    prompt_slots: dict[str, str] = Field(default_factory=dict)

    @field_validator("prompt_slots")
    @classmethod
    def valid_prompt_slots(cls,value):
        from osa.application.prompt_catalog import CATALOG, validate_template
        for key,text in value.items():
            if key not in CATALOG or len(text)>50000:raise ValueError("INVALID_PROMPT_SLOT")
            validate_template(text)
        return value
    output_filter: OutputFilterConfig = Field(default_factory=OutputFilterConfig)


def default_task(name: str) -> TaskConfig:
    if name == "work":
        return TaskConfig(max_tokens=8192,temperature=0.2,hard_timeout=90)
    if name == "observer":
        return TaskConfig(max_tokens=3000,temperature=0.2,hard_timeout=60)
    return TaskConfig()


def initial_configuration(settings: Any) -> RuntimeConfiguration:
    if settings.model_provider == "fake":
        return RuntimeConfiguration(task_configs={name: default_task(name) for name in TASK_NAMES})
    provider = ProviderConfig(
        id="default",
        name="默认 OpenAI 兼容服务",
        base_url=settings.model_base_url,
        api_key=settings.model_api_key,
        timeout=settings.model_timeout,
    )
    names = {
        "replyer": settings.reply_model or settings.chat_model,
        "planner": settings.thinking_model or settings.decision_model or settings.chat_model,
        "decision": settings.decision_model or settings.thinking_model or settings.chat_model,
        "memory": settings.memory_model or settings.chat_model,
        "utils": settings.chat_model,
        "vlm": settings.vision_model or settings.chat_model,
        "video": settings.vision_model or settings.chat_model,
        "voice": settings.audio_model,
        "embedding": settings.embedding_model,
    }
    unique = list(dict.fromkeys(value for value in names.values() if value))
    models = [
        ModelConfig(model_identifier=value, name=value, api_provider="default") for value in unique
    ]
    tasks = {name: default_task(name) for name in TASK_NAMES}
    for task, model in names.items():
        if model:
            tasks[task].model_list = [model]
    return RuntimeConfiguration(providers=[provider], models=models, task_configs=tasks)


class RuntimeConfigurationStore:
    def __init__(
        self, data_dir: str, initial: RuntimeConfiguration, memory_only: bool = False
    ) -> None:
        self._lock = threading.RLock()
        self._memory_only = memory_only
        self._value = initial
        self._directory = Path(data_dir) / "private"
        self._config_path = self._directory / "runtime-config.enc"
        self._key_path = self._directory / "runtime-config.key"
        self._fernet: Fernet | None = None
        if not memory_only:
            self._open_or_create()
        from osa.infrastructure.persona_growth import PersonaGrowthStore
        self.persona_growth = PersonaGrowthStore(self._directory, self._fernet, memory_only)

    def _open_or_create(self) -> None:
        self._directory.mkdir(parents=True, exist_ok=True)
        try:
            self._directory.chmod(0o700)
        except OSError:
            pass
        if self._key_path.exists():
            key = self._key_path.read_bytes().strip()
        else:
            key = Fernet.generate_key()
            fd = os.open(self._key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(key + b"\n")
        self._fernet = Fernet(key)
        if self._config_path.exists():
            try:
                raw = self._fernet.decrypt(self._config_path.read_bytes())
                self._value = RuntimeConfiguration.model_validate_json(raw)
                self._value.task_configs.setdefault("work", TaskConfig(max_tokens=8192, temperature=0.2, hard_timeout=90))
                self._value.task_configs.setdefault("observer", TaskConfig(max_tokens=3000, temperature=0.2, hard_timeout=60))
                self._value.task_configs.setdefault('decision', self._value.task_configs.get('planner', TaskConfig()).model_copy(deep=True))
                self._value.task_configs.setdefault('video', self._value.task_configs.get('vlm', TaskConfig()).model_copy(deep=True))
            except (InvalidToken, ValueError, OSError) as exc:
                raise RuntimeError("Encrypted runtime configuration is unreadable") from exc
        else:
            self._persist()

    def _persist(self) -> None:
        if self._memory_only:
            return
        assert self._fernet is not None
        payload = self._value.model_dump(mode="json")
        for row, provider in zip(payload["providers"], self._value.providers, strict=True):
            row["api_key"] = provider.api_key.get_secret_value()
        token = self._fernet.encrypt(json.dumps(payload, ensure_ascii=False).encode())
        temporary = self._config_path.with_suffix(".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(token)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self._config_path)

    def snapshot(self) -> RuntimeConfiguration:
        with self._lock:
            return self._value.model_copy(deep=True)

    def replace(
        self, value: RuntimeConfiguration, expected_revision: int | None = None
    ) -> RuntimeConfiguration:
        with self._lock:
            if expected_revision is not None and expected_revision != self._value.revision:
                raise ValueError("CONFIG_REVISION_CONFLICT")
            value.revision = self._value.revision + 1
            self._validate_references(value)
            self._value = value.model_copy(deep=True)
            self._persist()
            return self.snapshot()

    @staticmethod
    def _validate_references(value: RuntimeConfiguration) -> None:
        providers = {item.id for item in value.providers}
        models = {item.model_identifier for item in value.models}
        if len(providers) != len(value.providers) or len(models) != len(value.models):
            raise ValueError("DUPLICATE_CONFIGURATION_ID")
        if any(item.api_provider not in providers for item in value.models):
            raise ValueError("MODEL_PROVIDER_NOT_FOUND")
        if any(
            model not in models for task in value.task_configs.values() for model in task.model_list
        ):
            raise ValueError("TASK_MODEL_NOT_FOUND")

    def public(self) -> dict[str, Any]:
        value = self.snapshot().model_dump(mode="json")
        for provider, source in zip(value["providers"], self._value.providers, strict=True):
            provider["api_key"] = "********" if source.api_key.get_secret_value() else ""
            provider["has_api_key"] = bool(source.api_key.get_secret_value())
        return value

    def secret_values(self) -> list[str]:
        return [
            item.api_key.get_secret_value()
            for item in self.snapshot().providers
            if item.api_key.get_secret_value()
        ]
