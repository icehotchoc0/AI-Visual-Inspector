"""
Central configuration for AI-Visual-Inspector.

Everything is read from environment variables (see .env.example), same
pattern as AI-Portfolio-Agent's config.py: no hardcoded secrets, no
hardcoded paths outside this file.
"""
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Ollama / VLM
    ollama_host: str = "http://localhost:11434"
    vlm_model: str = "qwen2.5vl:7b"
    vlm_enabled: bool = True

    # Anomaly detection
    ad_backend: str = "patchcore"  # "patchcore" | "efficient_ad"
    ad_checkpoint_dir: str = "./backend/data/checkpoints"
    anomaly_threshold: float = 0.5

    # Dataset
    mvtec_root: str = "./backend/data/mvtec_ad"
    mvtec_categories: str = "bottle,cable,metal_nut,screw"

    # API
    api_host: str = "0.0.0.0"
    api_port: int = 8000

    # Optional email notification (mirrors AI-Portfolio-Agent)
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    notify_email: str = ""

    @property
    def categories(self) -> list[str]:
        return [c.strip() for c in self.mvtec_categories.split(",") if c.strip()]

    @property
    def checkpoint_dir_path(self) -> Path:
        return Path(self.ad_checkpoint_dir)

    @property
    def mvtec_root_path(self) -> Path:
        return Path(self.mvtec_root)


settings = Settings()
