"""
Central configuration for AI-Visual-Inspector (field-service edition).

Everything is read from environment variables / .env (see .env.example).
Settings prefixed INSPECTOR_ are deliberately namespaced so they never
collide with third-party env vars (e.g. Ollama's own OLLAMA_HOST, which
configures the *server* bind address, not a client target).
"""
from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # ---- Service -------------------------------------------------------
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    # Comma-separated list of accepted X-API-Key values. Empty => auth
    # disabled (local development only; the app logs a warning).
    api_keys: str = Field(default="", alias="INSPECTOR_API_KEYS")
    db_path: str = Field(default="./backend/data/inspector.db", alias="INSPECTOR_DB_PATH")
    categories_path: str = Field(default="./config/categories", alias="INSPECTOR_CATEGORIES_PATH")  # 폴더(권장) 또는 단일 YAML
    max_photos_per_job: int = 30
    max_image_bytes: int = 15 * 1024 * 1024
    # Comma-separated allowed browser origins for CORS. Empty = no browser
    # cross-origin access (service-to-service only). "*" = allow all (dev).
    cors_origins: str = Field(default="", alias="INSPECTOR_CORS_ORIGINS")
    # Requests per minute per API key (or client IP when auth is off). 0 = off.
    rate_limit_per_minute: int = Field(default=120, alias="INSPECTOR_RATE_LIMIT_PER_MINUTE")
    log_json: bool = Field(default=True, alias="INSPECTOR_LOG_JSON")
    log_level: str = Field(default="INFO", alias="INSPECTOR_LOG_LEVEL")
    metrics_enabled: bool = Field(default=True, alias="INSPECTOR_METRICS_ENABLED")
    # Longest side images are resized to before being sent to the VLM.
    vlm_image_max_side: int = 1568

    # ---- VLM providers ---------------------------------------------------
    # Ordered, comma-separated. First reachable provider wins; the next one
    # is tried on transport errors / refusals. "claude,ollama" = Claude API
    # primary with local Qwen2.5-VL fallback.
    vlm_providers: str = Field(default="claude,ollama", alias="INSPECTOR_VLM_PROVIDERS")
    vlm_enabled: bool = True

    # Claude (Anthropic SDK reads ANTHROPIC_API_KEY itself; this optional
    # override exists for containers that inject a differently named secret).
    anthropic_api_key: str = Field(default="", alias="INSPECTOR_ANTHROPIC_API_KEY")
    claude_model: str = Field(default="claude-opus-5-5", alias="INSPECTOR_CLAUDE_MODEL")
    claude_effort: str = Field(default="medium", alias="INSPECTOR_CLAUDE_EFFORT")  # low|medium|high|xhigh|max
    claude_max_tokens: int = 8192
    claude_timeout_s: float = 120.0

    # Ollama (local fallback)
    ollama_host: str = Field(default="http://localhost:11434", alias="INSPECTOR_OLLAMA_HOST")
    ollama_model: str = Field(default="qwen2.5vl:7b", alias="INSPECTOR_OLLAMA_MODEL")
    ollama_timeout_s: float = 180.0

    # ---- Optional anomaly-detection plugin (Anomalib) --------------------
    ad_backend: str = "patchcore"  # "patchcore" | "efficient_ad"
    ad_checkpoint_dir: str = "./backend/data/checkpoints"
    anomaly_threshold: float = 0.5

    # ---- Training data (see docs/DATA_SPEC.md) -----------------------------
    dataset_root: str = Field(default="./backend/data/datasets", alias="INSPECTOR_DATASET_ROOT")
    # Legacy MVTec AD benchmark settings (training/train_*.py)
    mvtec_root: str = "./backend/data/mvtec_ad"
    mvtec_categories: str = "bottle,cable,metal_nut,screw"

    # ---- derived -----------------------------------------------------------
    @property
    def api_key_set(self) -> set[str]:
        return {k.strip() for k in self.api_keys.split(",") if k.strip()}

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def provider_order(self) -> list[str]:
        return [p.strip().lower() for p in self.vlm_providers.split(",") if p.strip()]

    @property
    def categories(self) -> list[str]:
        return [c.strip() for c in self.mvtec_categories.split(",") if c.strip()]

    @property
    def checkpoint_dir_path(self) -> Path:
        return Path(self.ad_checkpoint_dir)

    @property
    def mvtec_root_path(self) -> Path:
        return Path(self.mvtec_root)

    @property
    def dataset_root_path(self) -> Path:
        return Path(self.dataset_root)


settings = Settings()
