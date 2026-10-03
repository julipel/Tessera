"""Публичный интерфейс модуля agent — единственная точка входа для других модулей."""

from app.modules.agent.application.loop import AgentLoop
from app.modules.agent.application.platform_prompt import PLATFORM_PROMPT_VERSION
from app.modules.agent.application.prompt import RuntimeContext, SystemPrompt, build_system_prompt
from app.modules.agent.domain.events import (
    AgentEvent,
    AnswerDelta,
    ComponentEmitted,
    FinishReason,
    ToolFinished,
    ToolStarted,
    TurnCompleted,
)
from app.modules.agent.domain.llm import (
    AssistantMessage,
    LLMChunk,
    LLMClient,
    LLMError,
    LLMMessage,
    LLMRequest,
    LLMResponse,
    ResponseCompleted,
    StopReason,
    TextDelta,
    ToolCall,
    ToolCallStarted,
    ToolResultMessage,
    ToolSchema,
    Usage,
    UserMessage,
)
from app.modules.agent.domain.turn import ToolExecutor, TurnContext, TurnLimits
from app.modules.agent.infrastructure.fake_llm import FakeLLM, FakeLLMExhaustedError, FakeReply
from app.modules.agent.infrastructure.tool_executor import RegistryToolExecutor

__all__ = [
    "PLATFORM_PROMPT_VERSION",
    "AgentEvent",
    "AgentLoop",
    "AnswerDelta",
    "AssistantMessage",
    "ComponentEmitted",
    "FakeLLM",
    "FakeLLMExhaustedError",
    "FakeReply",
    "FinishReason",
    "LLMChunk",
    "LLMClient",
    "LLMError",
    "LLMMessage",
    "LLMRequest",
    "LLMResponse",
    "RegistryToolExecutor",
    "ResponseCompleted",
    "RuntimeContext",
    "StopReason",
    "SystemPrompt",
    "TextDelta",
    "ToolCall",
    "ToolCallStarted",
    "ToolExecutor",
    "ToolFinished",
    "ToolResultMessage",
    "ToolSchema",
    "ToolStarted",
    "TurnCompleted",
    "TurnContext",
    "TurnLimits",
    "Usage",
    "UserMessage",
    "build_system_prompt",
]
