import os
import re
from pathlib import Path
from typing import Any, Optional
import yaml
from pydantic import BaseModel, Field, field_validator


class MediaLimit(BaseModel):
    max_bytes: int = 15728640
    max_seconds: Optional[int] = None
    allowed: bool = True


class KeyItem(BaseModel):
    key_env: str
    rpm_limit: int = 15
    rpd_limit: int = 500
    tpm_limit: int = 250000
    project: Optional[str] = None
    reserve: bool = False

    @field_validator("key_env")
    @classmethod
    def validate_key_env(cls, v: str) -> str:
        if v.startswith("AIzaSy"):
            raise ValueError(
                f"keys.yaml не должен содержать сырой ключ '{v[:8]}...'! "
                f"Укажите имя переменной окружения (например 'GEMINI_KEY_1')."
            )
        return v


class KeyConfig(BaseModel):
    key_env: str
    api_key: str
    rpm_limit: int = 15
    rpd_limit: int = 500
    tpm_limit: int = 250000
    project: Optional[str] = None
    reserve: bool = False


class MediaProbabilities(BaseModel):
    voice: float = 1.0
    video_note: float = 1.0
    video: float = 0.3
    photo: float = 0.3
    audio: float = 0.0
    animation: float = 0.0


class BotConfig(BaseModel):
    ALLOWED_CHAT_ID: int = 0
    OWNER_IDS: list[int] = Field(default_factory=list)
    MODEL_ID: str
    TZ: str = "Asia/Novosibirsk"
    BOT_ALIASES: list[str] = Field(default_factory=lambda: ["смешняв", "смешнявка"])
    SAFETY: str = "all OFF"
    PII_SCRUB: bool = True
    LOG_CONTENT: bool = False

    # Concurrency & priorities
    MAX_CONCURRENT: int = 4
    RPM_P0_RESERVE: int = 5
    BUDGET_P2_MAX: int = 70
    BUDGET_P1_MAX: int = 90

    # Ping policy
    PING_LIMIT: int = 20
    PING_WINDOW_S: int = 60
    PING_QUEUE_MAX: int = 30
    PING_QUEUE_TTL_S: int = 300
    RATE_NOTICE_COOLDOWN_S: int = 120
    TG_SEND_PER_MIN: int = 18
    TG_SEND_QUEUE_MAX: int = 100
    FOLLOWUP_WINDOW_S: int = 45

    # Proactive policy
    PROACTIVE_MIN: int = 10
    PROACTIVE_MAX: int = 25
    RANDOM_P: float = 0.15
    PROACTIVE_ACTIVE_MIN: int = 15
    COOLDOWN_S: int = 60
    PROACTIVE_MAX_PER_HOUR: int = 4
    PROACTIVE_MAX_PER_DAY: int = 15
    NIGHT_START: str = "03:00"
    NIGHT_END: str = "07:00"

    # Media policy
    MEDIA_AUTO_PER_DAY: int = 300
    MEDIA_AUTO_ANALYZE_P: MediaProbabilities = Field(default_factory=MediaProbabilities)
    MEDIA_AUTO_REACT_VIDEO_P: float = 0.05
    MEDIA_AUTO_REACT_AUDIO_P: float = 0.15
    MEDIA_AUTO_REACT_VOICE_P: float = 0.05
    MEDIA_AUTO_REACT_VIDEO_NOTE_P: float = 0.05
    MEDIA_AUTO_REACT_PHOTO_P: float = 0.03
    MEDIA_AUTO_REACT_ANIMATION_P: float = 0.03

    # Context & history
    CTX_LAST: int = 60
    SUMMARY_TOK: int = 800
    CHUNK_SUMMARY_TOK: int = 300
    THREAD_CHAIN_DEPTH: int = 3
    THREAD_NEIGHBORS: int = 10

    # Dossier
    DOSSIER_EVERY: int = 40
    DOSSIER_MAX_WINDOW: int = 120
    DOSSIER_CONSOLIDATE_EVERY: str = "24h"
    RECENT_TTL_DAYS: int = 30
    CLAIM_MIN_COUNT: int = 3
    DOSSIER_SLICES_MAX: int = 5
    STYLE_EXAMPLES_N: int = 8
    STYLE_FILTER_BATCH: int = 30

    # Research
    SEARCH_FORCE_TRIGGERS: list[str] = Field(
        default_factory=lambda: ["загугли", "погугли", "ресерч", "пруф", "чекни"]
    )
    LINK_TRIGGERS: list[str] = Field(
        default_factory=lambda: ["что за", "о чём", "пруф", "суть"]
    )
    LINK_TRIGGERS_SHOW: list[str] = Field(
        default_factory=lambda: ["загугли", "пруф", "дай ссылку", "источник"]
    )
    RESEARCH_TTL_H: int = 72
    RESEARCH_CACHE_MAX: int = 1000

    # Cache & retention
    MEDIA_CACHE_MAX: int = 2000
    MSG_RETENTION_DAYS: int = 30
    MSG_RETENTION_ROWS: int = 20000
    IMPORT_MAX_DAYS: int = 30
    IMPORT_DOSSIER_WINDOW: int = 400
    IMPORT_MAX_CALLS: int = 300
    STALE_UPDATE_S: int = 120

    # Operations
    SHADOW_MODE: bool = True
    ALERT_KEYS_DOWN_S: int = 600
    ALERT_DISK_MIN_MB: int = 500
    ALERT_COOLDOWN_S: int = 3600
    CONFLICT_MAX_WAIT_S: int = 900

    @field_validator("MODEL_ID")
    @classmethod
    def validate_model_id(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError(
                "Параметр MODEL_ID обязателен и не имеет значения по умолчанию! "
                "Укажите актуальную модель Gemini в config/bot.yaml (например, 'gemini-2.5-flash')."
            )
        return v.strip()

    @field_validator("IMPORT_MAX_DAYS")
    @classmethod
    def validate_import_max_days(cls, v: int, info) -> int:
        retention = info.data.get("MSG_RETENTION_DAYS", 30)
        if v > retention:
            raise ValueError(
                f"IMPORT_MAX_DAYS ({v}) не может превышать MSG_RETENTION_DAYS ({retention})!"
            )
        return v


class PersonaConfig(BaseModel):
    blocked_replies: list[str] = Field(default_factory=list)
    rate_limit_replies: list[str] = Field(default_factory=list)
    degraded_replies: list[str] = Field(default_factory=list)
    opt_out_replies: list[str] = Field(default_factory=list)


class AppConfig(BaseModel):
    bot: BotConfig
    persona: PersonaConfig
    keys: list[KeyConfig]
    limits: dict[str, Any]
    gen: dict[str, Any]
    tg_token: str = ""


def load_env_file(env_path: Path) -> dict[str, str]:
    """Parses a simple .env file into a dictionary."""
    env_vars = {}
    if not env_path.exists():
        return env_vars
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip("'\"")
                env_vars[k] = v
    return env_vars


def load_config(
    config_dir: str = "config",
    env_file: str = ".env",
    dry_run: bool = False
) -> AppConfig:
    """
    Loads all configuration files and environment secrets:
    1. Reads .env (or environment variables)
    2. Reads config/bot.yaml (MODEL_ID is required)
    3. Reads config/persona.yaml
    4. Reads config/limits.yaml
    5. Reads config/gen.yaml
    6. Loads and validates Gemini keys from keys.yaml + env auto-discovery
    """
    config_path = Path(config_dir)
    env_vars = load_env_file(Path(env_file))
    # System env overrides .env file
    for k, v in os.environ.items():
        env_vars[k] = v

    # 1. bot.yaml
    bot_yaml_path = config_path / "bot.yaml"
    if not bot_yaml_path.exists():
        raise FileNotFoundError(f"Файл конфигурации {bot_yaml_path} не найден!")
    with open(bot_yaml_path, "r", encoding="utf-8") as f:
        bot_raw = yaml.safe_load(f) or {}

    # Merge OWNER_IDS from env if available
    env_owners = env_vars.get("OWNER_IDS", "").strip()
    if env_owners:
        parsed_owners = []
        for p in env_owners.split(","):
            p = p.strip()
            if p.isdigit() or (p.startswith("-") and p[1:].isdigit()):
                parsed_owners.append(int(p))
        if parsed_owners:
            bot_raw["OWNER_IDS"] = parsed_owners

    bot_config = BotConfig(**bot_raw)

    # 2. persona.yaml
    persona_yaml_path = config_path / "persona.yaml"
    persona_raw = {}
    if persona_yaml_path.exists():
        with open(persona_yaml_path, "r", encoding="utf-8") as f:
            persona_raw = yaml.safe_load(f) or {}
    persona_config = PersonaConfig(**persona_raw)

    # 3. limits.yaml
    limits_yaml_path = config_path / "limits.yaml"
    limits_raw = {}
    if limits_yaml_path.exists():
        with open(limits_yaml_path, "r", encoding="utf-8") as f:
            limits_raw = yaml.safe_load(f) or {}

    # 4. gen.yaml
    gen_yaml_path = config_path / "gen.yaml"
    gen_raw = {}
    if gen_yaml_path.exists():
        with open(gen_yaml_path, "r", encoding="utf-8") as f:
            gen_raw = yaml.safe_load(f) or {}

    # 5. keys.yaml + env auto-discovery
    keys_yaml_path = config_path / "keys.yaml"
    configured_key_items: list[KeyItem] = []

    if keys_yaml_path.exists():
        with open(keys_yaml_path, "r", encoding="utf-8") as f:
            keys_raw = yaml.safe_load(f) or {}
        raw_list = keys_raw.get("keys", [])
        for item in raw_list:
            configured_key_items.append(KeyItem(**item))

    # Auto-discover GEMINI_KEY_* from env if not already configured in keys.yaml
    known_envs = {item.key_env for item in configured_key_items}
    for env_k in sorted(env_vars.keys()):
        if re.match(r"^GEMINI_KEY(_.*)?$", env_k) and env_k not in known_envs:
            configured_key_items.append(
                KeyItem(
                    key_env=env_k,
                    rpm_limit=15,
                    rpd_limit=500,
                    tpm_limit=250000,
                    project=None,
                    reserve=("RESERVE" in env_k.upper())
                )
            )

    # Resolve keys to values
    resolved_keys: list[KeyConfig] = []
    for item in configured_key_items:
        secret = env_vars.get(item.key_env, "").strip()
        if not secret:
            if dry_run:
                secret = f"fake_{item.key_env}"
            else:
                # Key is missing in env, skip with warning or ignore if dry_run
                continue
        resolved_keys.append(
            KeyConfig(
                key_env=item.key_env,
                api_key=secret,
                rpm_limit=item.rpm_limit,
                rpd_limit=item.rpd_limit,
                tpm_limit=item.tpm_limit,
                project=item.project,
                reserve=item.reserve
            )
        )

    tg_token = env_vars.get("TG_TOKEN", "").strip()

    return AppConfig(
        bot=bot_config,
        persona=persona_config,
        keys=resolved_keys,
        limits=limits_raw,
        gen=gen_raw,
        tg_token=tg_token
    )
