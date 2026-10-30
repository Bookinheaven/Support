"""Data models and abstractions for Support database tools."""

from dataclasses import dataclass, field
from typing import Any, Optional
from pydantic import BaseModel, Field


class ToolParameter(BaseModel):
    """Specification of an individual tool argument."""

    type: str
    description: str
    required: bool = True
    default: Optional[Any] = None
    items: Optional[dict[str, Any]] = None


class ToolDefinition(BaseModel):
    """Declarative specification of an application database tool."""

    name: str
    description: str
    parameters: dict[str, Any] = Field(default_factory=dict)

    def to_json_schema(self) -> dict[str, Any]:
        """Export as standard JSON schema."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass
class ToolCall:
    """Represents a structured tool invocation selected by the LLM or direct dispatcher."""

    tool: str
    arguments: dict[str, Any] = field(default_factory=dict)
    call_id: str = ""
    explanation: Optional[str] = None


@dataclass
class ToolResult:
    """Outcome of executing an application database tool."""

    tool: str
    success: bool
    output: Any = None
    display_text: Optional[str] = None
    error: Optional[str] = None
    requires_user_approval: bool = False
    change_plan: Optional[Any] = None
