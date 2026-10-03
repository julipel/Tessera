"""Агентный цикл (architecture.md §5). Реализация — P2-03, поведение задают
tests/test_agent_loop.py."""

from collections.abc import AsyncIterator

from app.modules.agent.domain.events import AgentEvent
from app.modules.agent.domain.llm import LLMClient
from app.modules.agent.domain.turn import ToolExecutor, TurnContext


class AgentLoop:
    def __init__(self, llm: LLMClient, tools: ToolExecutor) -> None:
        self._llm = llm
        self._tools = tools

    def run_turn(self, ctx: TurnContext) -> AsyncIterator[AgentEvent]:
        raise NotImplementedError("P2-03: агентный цикл ещё не реализован")
