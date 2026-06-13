"""Application configuration via Pydantic BaseSettings."""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
ANSWER_PLAN_DIR = BASE_DIR / "services" / "answer_plan"
QUERY_PLAN_DIR = BASE_DIR / "services" / "query_plan"


class LiteLLMSettings(BaseModel):
    proxy_api_base: str
    proxy_api_key: str
    agent_model: str
    stage_extractor_model: str
    username_moderation_model: str
    web_search_model: str
    reranker_model: str
    embedding_model: str
    embedding_dimension: int


class ApiSettings(BaseModel):
    segmentation_api_url: str
    segmentation_api_timeout_seconds: int
    offering_api_url: str
    offering_api_timeout_seconds: int
    common_qa_recommendation_api_url: str
    common_qa_recommendation_api_timeout_seconds: int
    ai_gate_auth_url: str
    ai_gate_search_url: str
    ai_gate_client_id: str
    ai_gate_client_secret: str
    kogito_endpoint: str
    kogito_endpoint_addressing_rule: str
    enrolment_form_base_url: str
    application_form_base_url: str


class DatabaseSettings(BaseModel):
    session_database_url: str
    milvus_host: str
    milvus_port: str
    milvus_user: str
    milvus_password: str
    milvus_database: str
    neo4j_uri: str
    neo4j_user: str
    neo4j_password: str
    mongo_db_uri: str
    mongo_db_name: str
    mongo_collection_name: str
    mongodb_max_pool_size: int
    mongodb_timeout: int


class RuntimeSettings(BaseModel):
    mcp_host: str
    mcp_port: int
    cache_ttl_seconds: int
    semantic_cache_threshold: float
    vector_db_threshold: float
    graph_db_relationship_depth: int
    answer_plan_dir: Path
    query_plan_dir: Path
    adk_artifact_root_dir: str
    log_level: str
    log_color: bool


class ObservabilitySettings(BaseModel):
    langfuse_public_key: str
    langfuse_secret_key: str
    langfuse_base_url: str
    agentops_api_key: str


class CRMIntegrationSettings(BaseSettings):
    crm_host: str
    crm_api_key: str
    crm_submit_form_endpoint: str
    crm_timeout_seconds: int


class Settings(BaseSettings):
    """Single source of truth for app settings (env/.env)."""

    # LiteLLM / models
    litellm_proxy_api_base: str = Field(default="", alias="LITELLM_PROXY_API_BASE")
    litellm_proxy_api_key: str = Field(default="", alias="LITELLM_PROXY_API_KEY")
    litellm_agent_model: str = Field(default="", alias="LITELLM_AGENT_MODEL")
    litellm_stage_extractor_model: str = Field(default="", alias="LITELLM_STAGE_EXTRACTOR_MODEL")
    litellm_username_moderation_model: str = Field(default="", alias="LITELLM_USERNAME_MODERATION_MODEL")
    litellm_web_search_model: str = Field(default="", alias="LITELLM_WEB_SEARCH_MODEL")
    reranker_model: str = Field(default="", alias="RERANKER_MODEL")
    embedding_model: str = Field(default="", alias="EMBEDDING_MODEL")
    embedding_dimension: int = Field(default=2048, alias="EMBEDDING_DIMENSION")

    # Runtime
    mcp_host: str = Field(default="", alias="MCP_HOST")
    mcp_port: int = Field(default=0, alias="MCP_PORT")
    cache_ttl_seconds: int = Field(default=3600, alias="CACHE_TTL_SECONDS")
    semantic_cache_threshold: float = Field(default=0.75, alias="SEMANTIC_CACHE_THRESHOLD")
    vector_db_threshold: float = Field(default=0.3, alias="VECTOR_DB_THRESHOLD")
    graph_db_relationship_depth: int = Field(default=2, alias="GRAPH_DB_RELATIONSHIP_DEPTH")
    adk_artifact_root_dir: str = Field(default="", alias="ADK_ARTIFACT_ROOT_DIR")
    query_agent_prompt_style: str = Field(default="new", alias="QUERY_AGENT_PROMPT_STYLE")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    log_color: bool = Field(default=True, alias="LOG_COLOR")

    # External APIs
    segmentation_api_url: str = Field(default="", alias="SEGMENTATION_API_URL")
    segmentation_api_timeout_seconds: int = Field(default=30, alias="SEGMENTATION_API_TIMEOUT_SECONDS")
    offering_api_url: str = Field(default="", alias="OFFERING_API_URL")
    offering_api_timeout_seconds: int = Field(default=30, alias="OFFERING_API_TIMEOUT_SECONDS")
    common_qa_recommendation_api_url: str = Field(
        default=(
            "https://edugate-dev.dxfuturetech.com.vn/knowledge-api/api/v1/chatbot/events/"
            "{event_id}/common-qa-recommendation"
        ),
        alias="COMMON_QA_RECOMMENDATION_API_URL",
    )
    common_qa_recommendation_api_timeout_seconds: int = Field(
        default=5,
        alias="COMMON_QA_RECOMMENDATION_API_TIMEOUT_SECONDS",
    )
    ai_gate_auth_url: str = Field(default="", alias="AI_GATE_AUTH_URL")
    ai_gate_search_url: str = Field(default="", alias="AI_GATE_SEARCH_URL")
    ai_gate_client_id: str = Field(default="", alias="AI_GATE_CLIENT_ID")
    ai_gate_client_secret: str = Field(default="", alias="AI_GATE_CLIENT_SECRET")
    kogito_endpoint: str = Field(default="", alias="KOGITO_ENDPOINT")
    kogito_endpoint_addressing_rule: str = Field(default="", alias="KOGITO_ENDPOINT_ADDRESSING_RULE")
    enrolment_form_base_url: str = Field(default="", alias="ENROLMENT_FORM_BASE_URL")
    application_form_base_url: str = Field(default="", alias="APPLICATION_FORM_BASE_URL")

    # Databases
    session_database_url: str = Field(default="", alias="SESSION_DATABASE_URL")
    milvus_host: str = Field(default="", alias="MILVUS_HOST")
    milvus_port: str = Field(default="", alias="MILVUS_PORT")
    milvus_user: str = Field(default="", alias="MILVUS_USER")
    milvus_password: str = Field(default="", alias="MILVUS_PASSWORD")
    milvus_database: str = Field(default="", alias="MILVUS_DATABASE")
    neo4j_uri: str = Field(default="", alias="NEO4J_URI")
    neo4j_user: str = Field(default="", alias="NEO4J_USER")
    neo4j_password: str = Field(default="", alias="NEO4J_PASSWORD")
    mongo_db_uri: str = Field(default="", alias="MONGO_DB_URI")
    mongo_db_name: str = Field(default="", alias="MONGO_DB_NAME")
    mongo_collection_name: str = Field(default="", alias="MONGO_COLLECTION_NAME")
    mongodb_max_pool_size: int = Field(default=10, alias="MONGODB_MAX_POOL_SIZE")
    mongodb_timeout: int = Field(default=5000, alias="MONGODB_TIMEOUT")

    # Observability / providers
    langfuse_public_key: str = Field(default="", alias="LANGFUSE_PUBLIC_KEY")
    langfuse_secret_key: str = Field(default="", alias="LANGFUSE_SECRET_KEY")
    langfuse_base_url: str = Field(default="", alias="LANGFUSE_BASE_URL")
    agentops_api_key: str = Field(default="", alias="AGENTOPS_API_KEY")
    google_api_key: str = Field(default="", alias="GOOGLE_API_KEY")
    google_genai_use_vertex_ai: bool = Field(default=False, alias="GOOGLE_GENAI_USE_VERTEX_AI")

    # Local paths / compatibility
    answer_plan_dir: Path = ANSWER_PLAN_DIR
    query_plan_dir: Path = QUERY_PLAN_DIR
    root_path: str = "/"

    # CRM integration
    crm_host: str = Field(default="", alias="CRM_HOST")
    crm_api_key: str = Field(default="", alias="CRM_API_KEY")
    crm_submit_form_endpoint: str = Field(default="", alias="CRM_SUBMIT_FORM_ENDPOINT")
    crm_timeout_seconds: int = Field(default=30, alias="CRM_TIMEOUT_SECONDS")

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    @property
    def litellm(self) -> LiteLLMSettings:
        return LiteLLMSettings(
            proxy_api_base=self.litellm_proxy_api_base,
            proxy_api_key=self.litellm_proxy_api_key,
            agent_model=self.litellm_agent_model,
            stage_extractor_model=self.litellm_stage_extractor_model,
            username_moderation_model=self.litellm_username_moderation_model,
            web_search_model=self.litellm_web_search_model,
            reranker_model=self.reranker_model,
            embedding_model=self.embedding_model,
            embedding_dimension=self.embedding_dimension,
        )

    @property
    def apis(self) -> ApiSettings:
        return ApiSettings(
            segmentation_api_url=self.segmentation_api_url,
            segmentation_api_timeout_seconds=self.segmentation_api_timeout_seconds,
            offering_api_url=self.offering_api_url,
            offering_api_timeout_seconds=self.offering_api_timeout_seconds,
            common_qa_recommendation_api_url=self.common_qa_recommendation_api_url,
            common_qa_recommendation_api_timeout_seconds=self.common_qa_recommendation_api_timeout_seconds,
            ai_gate_auth_url=self.ai_gate_auth_url,
            ai_gate_search_url=self.ai_gate_search_url,
            ai_gate_client_id=self.ai_gate_client_id,
            ai_gate_client_secret=self.ai_gate_client_secret,
            kogito_endpoint=self.kogito_endpoint,
            kogito_endpoint_addressing_rule=self.kogito_endpoint_addressing_rule,
            enrolment_form_base_url=self.enrolment_form_base_url,
            application_form_base_url=self.application_form_base_url,
        )

    @property
    def databases(self) -> DatabaseSettings:
        return DatabaseSettings(
            session_database_url=self.session_database_url,
            milvus_host=self.milvus_host,
            milvus_port=self.milvus_port,
            milvus_user=self.milvus_user,
            milvus_password=self.milvus_password,
            milvus_database=self.milvus_database,
            neo4j_uri=self.neo4j_uri,
            neo4j_user=self.neo4j_user,
            neo4j_password=self.neo4j_password,
            mongo_db_uri=self.mongo_db_uri,
            mongo_db_name=self.mongo_db_name,
            mongo_collection_name=self.mongo_collection_name,
            mongodb_max_pool_size=self.mongodb_max_pool_size,
            mongodb_timeout=self.mongodb_timeout,
        )

    @property
    def runtime(self) -> RuntimeSettings:
        return RuntimeSettings(
            mcp_host=self.mcp_host,
            mcp_port=self.mcp_port,
            cache_ttl_seconds=self.cache_ttl_seconds,
            semantic_cache_threshold=self.semantic_cache_threshold,
            vector_db_threshold=self.vector_db_threshold,
            graph_db_relationship_depth=self.graph_db_relationship_depth,
            answer_plan_dir=self.answer_plan_dir,
            query_plan_dir=self.query_plan_dir,
            adk_artifact_root_dir=self.adk_artifact_root_dir,
            log_level=self.log_level,
            log_color=self.log_color,
        )

    @property
    def observability(self) -> ObservabilitySettings:
        return ObservabilitySettings(
            langfuse_public_key=self.langfuse_public_key,
            langfuse_secret_key=self.langfuse_secret_key,
            langfuse_base_url=self.langfuse_base_url,
            agentops_api_key=self.agentops_api_key,
        )

    @property
    def crm(self) -> CRMIntegrationSettings:
        return CRMIntegrationSettings(
            crm_host=self.crm_host,
            crm_api_key=self.crm_api_key,
            crm_timeout_seconds=int(self.crm_timeout_seconds),
            crm_submit_form_endpoint=self.crm_submit_form_endpoint,
        )

    def validate_config(self) -> bool:
        required = {
            "LITELLM_PROXY_API_BASE": self.litellm_proxy_api_base,
            "LITELLM_PROXY_API_KEY": self.litellm_proxy_api_key,
            "LITELLM_AGENT_MODEL": self.litellm_agent_model,
            "EMBEDDING_MODEL": self.embedding_model,
            "MCP_HOST": self.mcp_host,
            "MCP_PORT": self.mcp_port,
        }
        missing = [name for name, value in required.items() if value in ("", None, 0)]
        if missing:
            logger.warning("Missing required config keys: %s", ", ".join(missing))
            return False
        logger.info("Configuration validated successfully")
        return True


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached Settings instance."""
    s = Settings()
    s.validate_config()
    return s


def reset_settings_cache() -> None:
    """Clear settings cache (useful for tests/hot-reload)."""
    get_settings.cache_clear()
