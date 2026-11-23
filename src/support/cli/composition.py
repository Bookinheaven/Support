"""CLI composition root for constructing application workflows."""

from support.application.assistant import AssistantWorkflow
from support.application.connection import ConnectionTestWorkflow
from support.application.ports import (
    CredentialStore,
    DatabaseConnector,
    LLMProvider,
    ProfileRepository,
    SchemaIntrospector,
)
from support.application.schema import SchemaIntrospectionWorkflow
from support.application.service import ProfileService
from support.application.tools import ToolRegistry
from support.infrastructure.dispatcher import ConnectorDispatcher
from support.infrastructure.keyring_credentials import KeyringCredentialStore
from support.infrastructure.ollama import OllamaClient
from support.infrastructure.sqlite_profiles import SQLiteProfileRepository


def get_profile_repository() -> ProfileRepository:
    """Construct standard SQLite profile repository."""
    return SQLiteProfileRepository()


def get_credential_store() -> CredentialStore:
    """Construct standard OS-native keyring credential store."""
    return KeyringCredentialStore()


def get_database_connector() -> DatabaseConnector:
    """Construct standard lazy connector dispatcher."""
    return ConnectorDispatcher()


def get_profile_service(
    repository: ProfileRepository | None = None,
    credential_store: CredentialStore | None = None,
) -> ProfileService:
    """Assemble ProfileService with required dependencies."""
    repo = repository or get_profile_repository()
    creds = credential_store or get_credential_store()
    return ProfileService(repository=repo, credential_store=creds)


def get_connection_workflow(
    repository: ProfileRepository | None = None,
    connector: DatabaseConnector | None = None,
    credential_store: CredentialStore | None = None,
) -> ConnectionTestWorkflow:
    """Assemble ConnectionTestWorkflow with required dependencies."""
    repo = repository or get_profile_repository()
    conn = connector or get_database_connector()
    creds = credential_store or get_credential_store()
    return ConnectionTestWorkflow(
        repository=repo,
        connector=conn,
        credential_store=creds,
    )


def get_llm_provider(
    base_url: str | None = None,
    default_model: str | None = None,
) -> LLMProvider:
    """Construct standard Ollama LLM provider."""
    return OllamaClient(base_url=base_url, default_model=default_model)


def get_tool_registry(
    schema_workflow: SchemaIntrospectionWorkflow | None = None,
    connection_workflow: ConnectionTestWorkflow | None = None,
) -> ToolRegistry:
    """Construct standard ToolRegistry wired to application workflows."""
    s_wf = schema_workflow or get_schema_workflow()
    c_wf = connection_workflow or get_connection_workflow()
    return ToolRegistry(schema_workflow=s_wf, connection_workflow=c_wf)


def get_assistant_workflow(
    llm_provider: LLMProvider | None = None,
    tool_registry: ToolRegistry | None = None,
) -> AssistantWorkflow:
    """Assemble AssistantWorkflow with LLM provider and tool registry."""
    provider = llm_provider or get_llm_provider()
    registry = tool_registry or get_tool_registry()
    return AssistantWorkflow(llm_provider=provider, tool_registry=registry)


def get_schema_workflow(
    repository: ProfileRepository | None = None,
    introspector: SchemaIntrospector | None = None,
    credential_store: CredentialStore | None = None,
) -> SchemaIntrospectionWorkflow:
    """Assemble SchemaIntrospectionWorkflow with required dependencies."""
    repo = repository or get_profile_repository()
    dispatcher = introspector or get_database_connector()
    creds = credential_store or get_credential_store()
    return SchemaIntrospectionWorkflow(
        repository=repo,
        introspector=dispatcher,
        credential_store=creds,
    )


def get_llm_provider(
    base_url: str | None = None,
    default_model: str | None = None,
) -> LLMProvider:
    """Construct standard Ollama LLM provider."""
    return OllamaClient(base_url=base_url, default_model=default_model)


def get_tool_registry(
    schema_workflow: SchemaIntrospectionWorkflow | None = None,
    connection_workflow: ConnectionTestWorkflow | None = None,
) -> ToolRegistry:
    """Construct standard ToolRegistry wired to application workflows."""
    s_wf = schema_workflow or get_schema_workflow()
    c_wf = connection_workflow or get_connection_workflow()
    return ToolRegistry(schema_workflow=s_wf, connection_workflow=c_wf)


def get_assistant_workflow(
    llm_provider: LLMProvider | None = None,
    tool_registry: ToolRegistry | None = None,
) -> AssistantWorkflow:
    """Assemble AssistantWorkflow with LLM provider and tool registry."""
    provider = llm_provider or get_llm_provider()
    registry = tool_registry or get_tool_registry()
    return AssistantWorkflow(llm_provider=provider, tool_registry=registry)
