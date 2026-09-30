"""Validated configuration. Secrets are masked in representations and errors."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, PositiveFloat, PositiveInt, SecretStr


class Config(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)
    bot_owners: set[PositiveInt] = Field(default_factory=set)
    user_names: dict[PositiveInt, Annotated[str, Field(max_length=80)]] = Field(
        default_factory=dict,
    )
    proactive_share_enabled: bool = True
    memory_profile_min_messages: PositiveInt = 10
    llm_provider: Literal["deepseek", "openai"] = "deepseek"
    deepseek_api_key: SecretStr = SecretStr("")
    deepseek_model: str = ""
    openai_api_key: SecretStr = SecretStr("")
    openai_model: str = ""
    vision_max_images: int = Field(3, ge=1, le=5)
    emotes_dir: str = "emotes"
    emotes_auto_collect: bool = True
    emotes_daily_import_limit: int = Field(20, ge=1, le=100)
    emotes_max_bytes: int = Field(5 * 1024 * 1024, ge=1024, le=20 * 1024 * 1024)
    search_provider: Literal["tavily"] = "tavily"
    search_api_key: SecretStr = SecretStr("")
    search_max_results: int = Field(5, ge=1, le=5)
    search_timeout_seconds: PositiveFloat = 15
    history_max_calls: int = Field(6, ge=1, le=12)
    search_max_calls: int = Field(2, ge=1, le=4)
    llm_timeout_seconds: PositiveFloat = 60
    request_timeout_seconds: PositiveFloat = 180
    llm_max_concurrency: PositiveInt = 2
    llm_max_output_tokens: PositiveInt = 2048
    queue_timeout_seconds: PositiveFloat = 3
    user_cooldown_seconds: PositiveFloat = 5
    session_ttl_seconds: PositiveFloat = 1800
    session_max_turns: PositiveInt = 8
    session_max_chars: PositiveInt = 12000
    input_max_chars: PositiveInt = 4000
    dedup_ttl_seconds: PositiveFloat = 300
    reply_chunk_chars: int = Field(1500, ge=300, le=4000)
    reply_max_messages: int = Field(3, ge=1, le=5)
    max_pending_requests: PositiveInt = 32

    @property
    def api_key(self) -> str:
        return getattr(self, f"{self.llm_provider}_api_key").get_secret_value().strip()

    @property
    def model(self) -> str:
        return getattr(self, f"{self.llm_provider}_model").strip()

    def validate_runtime(self) -> None:
        if not self.api_key or not self.model:
            raise ValueError(f"请配置 {self.llm_provider.upper()}_API_KEY 和对应 MODEL")
