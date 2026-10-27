from pydantic import BaseModel, Field, SecretStr, model_validator
from typing import Literal
from pydantic_settings import BaseSettings, SettingsConfigDict


class AdminAccount(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    token: SecretStr
    role: Literal["admin", "moderator", "reviewer", "viewer"]
    groups: list[str] = Field(default_factory=list)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OSA_", env_file=".env", extra="ignore")

    database_url: str = "sqlite+aiosqlite:///./osa.db"
    auto_create_schema: bool = False
    admin_token: SecretStr
    napcat_token: SecretStr
    admin_accounts: list[AdminAccount] = Field(default_factory=list)
    bot_id: str = "99999"
    adapter_id: str = "napcat-primary"
    allowed_groups: list[str] = Field(default_factory=list)
    private_enabled: bool = False
    demo_mode: bool = False
    model_provider: str = "fake"
    model_base_url: str = "https://api.openai.com/v1"
    model_api_key: SecretStr = SecretStr("")
    chat_model: str = "configured-chat-model"
    decision_model: str | None = None
    thinking_model: str | None = None
    reply_model: str | None = None
    memory_model: str | None = None
    global_group_memory: bool = False
    observer_model: str | None = None
    observer_base_url: str | None = None
    observer_api_key: SecretStr = SecretStr("")
    max_daily_observer_calls: int = Field(default=0, ge=0, le=1000)
    # The unified stream/thinking runtime is the default architecture.  Set
    # OSA_V3_ENABLED=false only for a deliberate rollback while migrating data.
    v3_enabled: bool = True
    decision_mode: Literal["classifier", "llm", "hybrid", "local"] = "llm"
    reply_mode: Literal["staged", "integrated"] = "integrated"
    decision_confidence_threshold: float = Field(default=.8, ge=0, le=1)
    thinking_max_steps: int = Field(default=12, ge=1, le=24)
    thinking_max_seconds: float = Field(default=180, ge=1, le=600)
    coding_provider: Literal["builtin", "opencode", "codex"] = "builtin"
    opencode_endpoint: str = ""
    opencode_password: SecretStr = SecretStr("")
    opencode_username: str = "opencode"
    opencode_version: str = "1.2.27"
    opencode_isolation_confirmed: bool = False
    opencode_managed_gateway: bool = True
    codex_endpoint: str = ""
    codex_password: SecretStr = SecretStr("")
    codex_username: str = "codex"
    codex_version: str = "0.147.0"
    codex_isolation_confirmed: bool = False
    codex_managed_gateway: bool = True
    model_timeout: float = Field(default=30.0, gt=0, le=120)
    max_reply_chars: int = Field(default=800, ge=50, le=4000)
    max_input_chars: int = Field(default=6000, ge=100, le=20000)
    max_context_chars: int = Field(default=20000, ge=1000, le=100000)
    # 0 disables the daily model-call budget. A positive value keeps the
    # persistent daily quota for installations that want cost control.
    max_daily_model_calls: int = Field(default=0, ge=0, le=100000)
    worker_concurrency: int = Field(default=10, ge=1, le=32)
    task_concurrency: int = Field(default=3, ge=1, le=16)
    database_pool_size: int = Field(default=5, ge=1, le=20)
    ws_action_timeout: float = Field(default=15.0, gt=0, le=60)
    host: str = "127.0.0.1"
    port: int = 8080
    echo_token: SecretStr = SecretStr("")
    v2_enabled: bool = True
    v2_database_url: str = "./data/registry.db"
    work_enabled: bool = False
    workspace_dir: str = "./data/workspaces"
    nightly_research_limit: int = Field(default=20, ge=0, le=100)
    runtime_mode: Literal["normal", "lowmem", "custom"] = "custom"
    data_dir: str = "./data"
    features_enabled: bool = True
    bot_skills_enabled: bool = False
    qq_admin_ids: list[str] = Field(default_factory=list)
    retention_days: int = Field(default=30, ge=1, le=3650)
    nightly_hour: int = Field(default=3, ge=0, le=23)
    embedding_base_url: str | None = None
    embedding_api_key: SecretStr = SecretStr("")
    embedding_model: str = "configured-embedding-model"
    embedding_dimension: int = Field(default=1024, ge=32, le=8192)
    vision_model: str | None = None
    audio_model: str = "whisper-1"
    tts_model: str = "tts-1"
    media_enabled: bool = False
    max_download_bytes: int = Field(default=20_000_000, ge=1024, le=100_000_000)
    object_store: str = "local"
    s3_endpoint: str | None = None
    s3_bucket: str = "osa"
    research_enabled: bool = False
    search_provider: str = "searxng"
    search_base_url: str = "http://searxng:8080"
    search_api_key: SecretStr = SecretStr("")
    deepseek_web_enabled: bool = False
    browser_worker_url: str = "http://browser-worker:8090"
    browser_vnc_url: str = "http://browser-worker:6080"
    worker_token: SecretStr = SecretStr("")
    browser_cookie_secure: bool = False
    deepseek_trigger: str = "always"
    sandbox_enabled: bool = False
    group_file_download_enabled: bool = True
    sandbox_worker_url: str = "http://sandbox-worker:8091"
    indexer_worker_url: str = "http://indexer-worker:8092"
    screenshot_worker_url: str = "http://screenshot-worker:8093"
    qqbot_api_url: str = ""
    qqbot_timeout: float = Field(default=600.0, ge=30, le=900)
    qqbot_tools_cache_seconds: int = Field(default=86400, ge=0, le=86400)
    qzone_enabled: bool = False
    qzone_adapter_url: str | None = None
    qzone_adapter_token: SecretStr = SecretStr("")
    max_daily_research_calls: int = Field(default=30, ge=1, le=1000)

    @model_validator(mode="after")
    def validate_deployment(self) -> "Settings":
        if any(len(s.get_secret_value()) < 16 for s in (self.admin_token, self.napcat_token)):
            raise ValueError("Admin and NapCat tokens must contain at least 16 characters")
        if self.admin_token == self.napcat_token:
            raise ValueError("Admin and NapCat tokens must be different")
        tokens = [self.admin_token.get_secret_value(), self.napcat_token.get_secret_value()]
        for account in self.admin_accounts:
            value = account.token.get_secret_value()
            if len(value) < 24 or value in tokens or any(group not in self.allowed_groups for group in account.groups):
                raise ValueError("Account tokens must be distinct and at least 24 characters; groups must be allowlisted")
            tokens.append(value)
        if self.model_provider not in {"fake", "openai"}:
            raise ValueError("model_provider must be fake or openai")
        if self.model_provider == "fake" and not self.demo_mode:
            raise ValueError("FakeModel requires demo_mode=true")
        if self.database_url == "memory://" and not self.demo_mode:
            raise ValueError("Volatile memory:// storage is restricted to demo mode")
        if self.model_provider == "openai" and not self.model_api_key.get_secret_value():
            raise ValueError("OpenAI-compatible provider requires a configured API key")
        if (not self.bot_id.isdecimal() or any(not x.isdecimal() for x in self.allowed_groups)
                or any(not x.isdecimal() for x in self.qq_admin_ids)):
            raise ValueError("QQ and group IDs must be decimal strings")
        if (self.deepseek_web_enabled or self.sandbox_enabled) and len(self.worker_token.get_secret_value()) < 24:
            raise ValueError("Browser and sandbox workers require a worker token of at least 24 characters")
        if self.deepseek_trigger not in {"explicit", "freshness", "always"}:
            raise ValueError("deepseek_trigger must be explicit, freshness or always")
        if self.object_store not in {"local", "s3"}:
            raise ValueError("object_store must be local or s3")
        if self.v3_enabled and not self.v2_enabled:
            raise ValueError("V3 requires the persistent task registry (v2_enabled)")
        if self.coding_provider == "opencode" and (not self.v3_enabled or "{workspace_id}" not in self.opencode_endpoint or not self.opencode_password.get_secret_value() or self.opencode_version != "1.2.27" or (not self.opencode_managed_gateway and not self.opencode_isolation_confirmed)):
            raise ValueError("OpenCode requires V3, a version-locked authenticated endpoint and confirmed per-workspace isolation")
        if self.coding_provider == "codex" and (not self.v3_enabled or "{workspace_id}" not in self.codex_endpoint or not self.codex_password.get_secret_value() or self.codex_version != "0.147.0" or (not self.codex_managed_gateway and not self.codex_isolation_confirmed)):
            raise ValueError("Codex CLI requires V3, a version-locked authenticated endpoint and confirmed per-workspace isolation")
        if self.observer_model and not self.demo_mode and not self.observer_api_key.get_secret_value():
            raise ValueError("The independent observer requires its own configured API key")
        return self
