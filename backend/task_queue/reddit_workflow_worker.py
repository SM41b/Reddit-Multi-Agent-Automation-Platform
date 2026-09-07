"""
Reddit workflow queue worker — routes jobs from the existing BullMQ-based
task_queue/queue_manager.py (fully reusable as-is, no changes needed there)
into the right persona's HITLRedditOrchestrator.

Why a queue instead of running the orchestrator inline in the API request
(what reddit_workflow_api.py's /start endpoint did originally): a workflow
run can pause indefinitely at the approval_gate interrupt waiting on a
human — an HTTP request can't sit open for that. The job returns
"awaiting_approval" or "completed" as its result; the caller polls
GET /api/workflow/{persona_id}/jobs/{job_id} instead of waiting on the
original request.
"""
from typing import Dict, Any, Optional
from loguru import logger

from task_queue.queue_manager import queue_manager
from agents.agent_factory import AgentFactory

QUEUE_NAME = "reddit_workflow_tasks"

_factory: Optional[AgentFactory] = None


def set_factory(factory: AgentFactory) -> None:
    """Called once from reddit_platform_startup.py after the factory is
    built, so this worker (running in the same process) can reach it."""
    global _factory
    _factory = factory


async def process_reddit_workflow_job(job) -> Dict[str, Any]:
    """Handler registered with queue_manager for QUEUE_NAME. `job` is a
    BullMQ Job object; job.data is whatever dict was passed to add_job()."""
    if _factory is None:
        raise RuntimeError("reddit_workflow_worker.set_factory() was never called — "
                            "check reddit_platform_startup.py wiring")

    data = job.data
    persona_id = data["persona_id"]
    thread_id = data["thread_id"]

    orchestrator = _factory.get_orchestrator(persona_id)
    if orchestrator is None:
        raise ValueError(f"Unknown persona '{persona_id}'")

    initial_state = {
        "messages": [],
        "task_brief": data.get("task_brief", ""),
        "shared_context": data.get("context", {}),
        "agent_outputs": {},
        "workflow_phase": "started",
        "current_agent": "",
        "iteration_count": 0,
        "confidence_scores": {},
        "pending_approvals": [],
        "approval_checkpoints": {},
        "human_feedback": {},
        "requires_approval": False,
        "errors": [],
        "execution_path": [],
        "workspace_id": persona_id,
        "user_id": data.get("user_id", "default"),
        "thread_id": thread_id,
    }
    config = {"configurable": {"thread_id": thread_id}}

    async for _ in orchestrator.workflow.astream(initial_state, config=config):
        pass

    snapshot = await orchestrator.workflow.aget_state(config)
    awaiting_approval = bool(snapshot.next)

    result = {
        "status": "awaiting_approval" if awaiting_approval else "completed",
        "thread_id": thread_id,
        "persona_id": persona_id,
    }
    logger.info(f"Reddit workflow job {job.id} for persona {persona_id}: {result['status']}")
    return result


async def register_reddit_workflow_queue() -> None:
    """Call once at startup, after queue_manager itself is connected/
    initialized elsewhere (main.py already does this for its own queues —
    this just adds one more queue + worker to the same manager)."""
    await queue_manager.create_queue(QUEUE_NAME)
    await queue_manager.register_worker(QUEUE_NAME, process_reddit_workflow_job)
    logger.info(f"Registered worker for queue: {QUEUE_NAME}")