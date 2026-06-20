from pydantic import computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Database
    database_url: str  # asyncpg DSN, e.g. postgresql+asyncpg://user:pass@host/db

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # JWT — RS256
    # Generate: openssl genrsa -out jwt_private.pem 2048
    #           openssl rsa -in jwt_private.pem -pubout -out jwt_public.pem
    # Set JWT_PRIVATE_KEY / JWT_PUBLIC_KEY to PEM file contents (newlines preserved).
    jwt_private_key: str  # RSA private key PEM — REST only (signing)
    jwt_public_key: str  # RSA public key PEM  — REST + Shell (verification)
    algorithm: str = "RS256"
    access_token_expire_minutes: int = 525600  # 1 year
    refresh_token_expire_days: int = 30

    # OAuth — Yandex
    yandex_client_id: str = ""
    yandex_client_secret: str = ""
    yandex_redirect_uri: str = ""

    # OAuth — GitHub
    github_client_id: str = ""
    github_client_secret: str = ""
    github_redirect_uri: str = ""

    # S3 / Garage
    s3_endpoint_url: str = ""
    s3_access_key_id: str = ""
    s3_secret_access_key: str = ""
    s3_bucket: str = "openclaw"
    s3_region: str = "us-east-1"

    # NATS
    nats_url: str = "nats://localhost:4222"

    # RAG service
    rag_service_url: str = "http://localhost:8001"
    rag_timeout_seconds: int = 10

    # Meilisearch
    meilisearch_url: str = "http://localhost:7700"
    meilisearch_api_key: str = ""

    # Speaches (Whisper HTTP server, OpenAI-compatible)
    speaches_url: str = "http://localhost:8001"
    speaches_model: str = "Systran/faster-whisper-large-v3"
    speaches_timeout_seconds: int = 300  # audio can be long

    # OpenAI-compatible proxy (used by openai proxy endpoint, not main chat flow)
    openai_gateway_url: str = "http://localhost:8080"

    # GoClaw gateway (admin provisioning API)
    goclaw_gateway_url: str = ""
    goclaw_gateway_token: str = ""
    goclaw_default_model: str = "qwen3"
    goclaw_provider_name: str = "litellm"  # Display name for the LiteLLM provider
    # LiteLLM base URL as seen by GoClaw (internal network, e.g. http://litellm:4000/v1)
    goclaw_litellm_url: str = ""
    goclaw_litellm_api_key: str = ""
    # Public-facing LiteLLM URL shown in GoClaw provider api_base (e.g. http://1.2.3.4:4000)
    # Falls back to goclaw_litellm_url if not set
    goclaw_litellm_api_base: str = ""
    # MCP server URL as seen by GoClaw (e.g. http://machine2:8002)
    goclaw_mcp_url: str = ""

    # Frontend (used for OAuth redirects, CORS)
    # Set CORS_ORIGINS to override; falls back to FRONTEND_URL.
    # Multiple origins: comma-separated, e.g. "https://app.example.com,http://localhost:3000"
    frontend_url: str = "http://localhost:3000"
    cors_origins: str = ""

    @computed_field
    @property
    def cors_origins_list(self) -> list[str]:
        raw = self.cors_origins or self.frontend_url
        return [o.strip() for o in raw.split(",") if o.strip()]

    # Logging
    log_level: str = "INFO"

    # Conversation RAG indexing threshold (messages before auto-index)
    rag_index_threshold: int = 20

    # MCP service shared secret (used by mcp/ service to call REST API on behalf of users)
    mcp_service_key: str = ""

    # GoClaw WebSocket RPC (replaces shell service)
    goclaw_gateway_ws_url: str = "ws://goclaw:18790/ws"

    # GoClaw webhook secret — shared with GoClaw for turn_end and file-created webhooks
    goclaw_webhook_secret: str = ""

    # Tools service — lightweight FastAPI service for GoClaw HTTP tools
    tools_service_url: str = "http://tools:8003"
    tools_service_key: str = ""

    # Background GoClaw sync (session → conversation reconciliation)
    goclaw_sync_interval_seconds: int = 300  # 0 = disabled

    # GoClaw skills
    # Comma-separated names of catalog skills to grant to every new workspace agent
    # e.g. "python-runner,web-scraper"
    goclaw_default_skills: str = ""
    # Path to directory of .md skill manifests (each file → ZIP uploaded to GoClaw)
    # Leave empty to disable custom skill upload. Mount ./skills:/skills in Docker.
    goclaw_skills_dir: str = ""
    # MCP tool prefix: tools are registered as "{prefix}__{tool_name}" in GoClaw
    # Default "ws" → ws__rag_search, ws__list_files, etc.
    goclaw_mcp_tool_prefix: str = "ws"
    # Embedding model for GoClaw vector memory and knowledge graph.
    # Must match a model routed through LiteLLM (e.g. Qwen3-Embedding via Ollama).
    # Stored as settings.embedding.model on the provider and in system-configs.
    # Leave empty to skip embedding configuration.
    goclaw_embedding_model: str = "text-embedding-qwen3"
    # Agent personality — if set, GoClaw LLM-summons SOUL.md/IDENTITY.md from this description
    goclaw_agent_description: str = ""
    # Agent type: "open" (per-user context files) or "predefined" (shared context)
    goclaw_agent_type: str = "open"

    # TTS default: provider applied to every new workspace agent.
    # "openai" = route through LiteLLM → Speaches/Kokoro (recommended when LiteLLM is configured).
    # "edge"   = free Microsoft Edge TTS (requires edge-tts CLI in GoClaw container).
    # "off"    = no TTS.
    goclaw_tts_provider: str = "openai"
    # auto mode: "off" | "always" | "inbound" (reply with voice when user sent voice) | "tagged"
    goclaw_tts_auto: str = "inbound"
    # Voice for openai/Speaches provider (Kokoro voices: af_heart, af_sky, am_adam, ...)
    goclaw_tts_voice: str = "af_heart"
    # Edge TTS voice (fallback for Telegram/opus) — see `edge-tts --list-voices`
    goclaw_tts_edge_voice: str = "en-US-JennyNeural"


settings = Settings()
