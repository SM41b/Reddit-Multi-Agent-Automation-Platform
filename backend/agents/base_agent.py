"""
Lightweight base agent, replacing the reference repo's agents/base_agent.py.

Two changes from the original:

1. Dropped the hard dependency on memory.memory_manager.MemoryManager
   (Neo4j + Qdrant). Your project runs Postgres + Redis, not a graph/vector
   memory stack, and HITLRedditOrchestrator already passes each agent its
   shared_context and previous_outputs directly through the task dict — so
   agents don't need to fetch their own memory. If you later add real
   cross-run memory, plug it in as an optional store rather than reviving
   the Neo4j/Qdrant dependency.

2. Dropped the agent-level approval_manager.create_approval_request() call
   inside execute(). The original had every agent request its own approval
   on low confidence — but HITLRedditOrchestrator's hitl_checkpoint node
   already does that centrally, using this exact confidence score. Keeping
   both would double up on approval logic. This class just returns
   {"output": ..., "confidence": ...} and lets the orchestrator decide.

services/llm_service.py is unchanged and reused as-is — it has no
Supabase/Neo4j coupling.
"""
from abc import ABC, abstractmethod
from typing import Dict, Any, Optional, List
from enum import Enum
import asyncio
import json
from datetime import datetime
from loguru import logger

from services.llm_service import llm_service, LLMProvider


class AgentStatus(Enum):
    IDLE = "idle"
    WORKING = "working"
    COMPLETED = "completed"
    ERROR = "error"


class BaseAgent(ABC):
    """Base class for Research/Content/Analysis agents.

    Contract with HITLRedditOrchestrator: execute(task) -> Dict with at
    least "output" (Dict) and "confidence" (float) keys. The orchestrator
    handles retries at the workflow level via asyncio.wait_for + its own
    error_handler node, but this class also retries at the agent level for
    transient LLM failures, same as the original.
    """

    def __init__(self, name: str, role: str, personality: Optional[Dict[str, Any]] = None):
        self.name = name
        self.role = role
        self.personality = personality or {}
        self.status = AgentStatus.IDLE
        self.retry_limit = self.personality.get("retry_limit", 3)
        self.current_task: Optional[Dict[str, Any]] = None
        self.tools: List[Any] = []
        logger.info(f"Initialized {name} agent with role: {role}")

    @abstractmethod
    async def process_task(self, task: Dict[str, Any]) -> Dict[str, Any]:
        """Do the actual work. Must return {"output": {...}, "confidence": float}."""
        raise NotImplementedError

    def get_system_prompt(self) -> str:
        return f"You are {self.name}, a {self.role}. {self.personality.get('description', '')}"

    async def execute(self, task: Dict[str, Any]) -> Dict[str, Any]:
        """Called directly by HITLRedditOrchestrator._execute_agent()."""
        self.current_task = task
        self.status = AgentStatus.WORKING

        last_error: Optional[Exception] = None
        for attempt in range(self.retry_limit):
            try:
                logger.info(f"{self.name} starting task attempt {attempt + 1}")
                result = await self.process_task(task)
                result.setdefault("agent", self.name)
                result.setdefault("timestamp", datetime.now().isoformat())
                self.status = AgentStatus.COMPLETED
                return result
            except Exception as e:
                last_error = e
                logger.error(f"{self.name} attempt {attempt + 1} failed: {e}")
                if attempt < self.retry_limit - 1:
                    await asyncio.sleep(2 ** attempt)

        self.status = AgentStatus.ERROR
        return {"output": {}, "confidence": 0.0, "agent": self.name, "error": str(last_error)}

    def get_status(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "role": self.role,
            "status": self.status.value,
            "current_task": (self.current_task or {}).get("id"),
        }

    # --- Shared LLM helper -------------------------------------------

    async def _generate(self, prompt: str, context: Optional[Dict[str, Any]] = None,
                         temperature: float = 0.7) -> Dict[str, Any]:
        """Call the LLM and parse a structured response. context here is the
        task's shared_context/previous_outputs, passed explicitly by the
        subclass — not fetched from a memory manager."""
        messages = [
            {"role": "system", "content": self.get_system_prompt()},
            {"role": "user", "content": self._build_prompt(prompt, context or {})},
        ]
        try:
            response = await llm_service.generate_response(
                messages=messages,
                agent_name=self.name,
                temperature=temperature,
                max_tokens=4000,
                preferred_provider=LLMProvider.ANTHROPIC,
            )
            return self._parse_response(response.content, response.confidence)
        except Exception as e:
            logger.error(f"{self.name} LLM generation failed: {e}")
            return {"content": "", "confidence": 0.0, "error": str(e)}

    def _build_prompt(self, prompt: str, context: Dict[str, Any]) -> str:
        context_str = f"\n\nContext:\n{json.dumps(context, indent=2)}" if context else ""
        return f"{prompt}{context_str}\n\nRespond with structured JSON and a confidence score."

    def _parse_response(self, content: str, confidence: float) -> Dict[str, Any]:
        try:
            if "{" in content and "}" in content:
                start, end = content.find("{"), content.rfind("}") + 1
                parsed = json.loads(content[start:end])
                return {"content": content, "structured_output": parsed, "confidence": confidence}
        except Exception:
            pass
        return {"content": content, "confidence": confidence}