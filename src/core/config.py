from pydantic import AnyHttpUrl
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Database
    database_url: str  # asyncpg DSN, e.g. postgresql+asyncpg://user:pass@host/db

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # JWT
    secret_key: str
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 60
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
    # LiteLLM base URL as seen by GoClaw (internal network, e.g. http://litellm:4000/v1)
    goclaw_litellm_url: str = ""
    goclaw_litellm_api_key: str = ""
    # MCP server URL as seen by GoClaw (e.g. http://machine2:8002)
    goclaw_mcp_url: str = ""

    # Frontend (used for OAuth redirects, CORS)
    frontend_url: str = "http://localhost:3000"

    # Logging
    log_level: str = "INFO"

    # Conversation RAG indexing threshold (messages before auto-index)
    rag_index_threshold: int = 20

    # MCP service shared secret (used by mcp/ service to call REST API on behalf of users)
    mcp_service_key: str = ""


settings = Settings()
