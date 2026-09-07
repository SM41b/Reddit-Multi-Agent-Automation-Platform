"""
API layer for the Reddit multi-agent orchestrator — new file, no direct
equivalent in the reference repo (workflow_controller.py there drives a
different orchestrator/state shape entirely).

Endpoints:
  POST /api/personas                                             create + authenticate a persona
  GET  /api/personas/{persona_id}/status                          agent statuses for a persona
  POST /api/workflow/{persona_id}/start                            enqueue an orchestrator run
  GET  /api/workflow/{persona_id}/jobs/{job_id}                    poll a queued run's status/result
  GET  /api/workflow/{persona_id}/approvals                        list pending approvals
  POST /api/workflow/{persona_id}/approvals/{approval_id}/respond  approve/reject, resumes the run

Dependency injection follows FastAPI's standard app.state pattern — the
shared ApprovalStore/Notifier/BehaviorProfileManager/AgentFactory instances
are created once at app startup (see reddit_platform_startup.py) and
pulled from request.app.state here, not constructed per-request.
"""
from typing import Dict, Any, Optional
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from loguru import logger

from agents.agent_factory import AgentFactory, PersonaConfig
from task_queue.queue_manager import queue_manager
from task_queue import reddit_workflow_worker

router = APIRouter(prefix="/api", tags=["reddit-orchestrator"])


# --- Request/response models ------------------------------------------

class CreatePersonaRequest(BaseModel):
    persona_id: str
    reddit_client_id: str
    reddit_client_secret: str
    reddit_username: str
    reddit_password: str
    default_subreddit: Optional[str] = None
    personality: Dict[str, Any] = {}


class StartWorkflowRequest(BaseModel):
    task_brief: str
    user_id: str = "default"
    # e.g. {"subreddit": "...", "action_type": "submit_post" | "reply_comment", "comment_id": "..."}
    context: Dict[str, Any] = {}


class ApprovalResponseRequest(BaseModel):
    thread_id: str
    decision: str          # "approved" | "rejected"
    feedback: str = ""
    responded_by: str = "unknown"


# --- Helpers -------------------------------------------------------------

def _get_factory(request: Request) -> AgentFactory:
    factory = getattr(request.app.state, "agent_factory", None)
    if factory is None:
        raise HTTPException(status_code=500, detail="AgentFactory not initialized on app startup")
    return factory


def _get_orchestrator(request: Request, persona_id: str):
    factory = _get_factory(request)
    orchestrator = factory.get_orchestrator(persona_id)
    if orchestrator is None:
        raise HTTPException(status_code=404, detail=f"Unknown persona '{persona_id}'")
    return orchestrator


# --- Persona lifecycle -----------------------------------------------------

@router.post("/personas")
async def create_persona(body: CreatePersonaRequest, request: Request) -> Dict[str, Any]:
    factory = _get_factory(request)
    try:
        await factory.create_persona(PersonaConfig(
            persona_id=body.persona_id,
            reddit_client_id=body.reddit_client_id,
            reddit_client_secret=body.reddit_client_secret,
            reddit_username=body.reddit_username,
            reddit_password=body.reddit_password,
            default_subreddit=body.default_subreddit,
            personality=body.personality,
        ))
    except RuntimeError as e:
        # Reddit auth failure — a 400, not a 500, since it's a bad-credentials
        # problem the caller can fix and retry.
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": "created", "persona_id": body.persona_id}


@router.get("/personas/{persona_id}/status")
async def persona_status(persona_id: str, request: Request) -> Dict[str, Any]:
    factory = _get_factory(request)
    statuses = factory.get_agent_statuses(persona_id)
    if not statuses:
        raise HTTPException(status_code=404, detail=f"Unknown persona '{persona_id}'")
    return {"persona_id": persona_id, "agents": statuses}


# --- Workflow execution -----------------------------------------------

@router.post("/workflow/{persona_id}/start")
async def start_workflow(persona_id: str, body: StartWorkflowRequest, request: Request) -> Dict[str, Any]:
    """Enqueues the run rather than executing it inline — a run can pause
    indefinitely at the approval_gate interrupt waiting on a human, which
    an open HTTP request can't do, and a Redis/worker hiccup should retry
    rather than fail the request outright. Poll the returned job_id via
    GET /api/workflow/{persona_id}/jobs/{job_id}, or watch the
    "dashboard_notifications" event_bus topic for push updates."""
    _get_orchestrator(request, persona_id)  # 404s early if the persona doesn't exist
    thread_id = str(uuid4())

    job_id = await queue_manager.add_job(
        reddit_workflow_worker.QUEUE_NAME,
        data={
            "persona_id": persona_id,
            "thread_id": thread_id,
            "task_brief": body.task_brief,
            "context": body.context,
            "user_id": body.user_id,
        },
    )
    if job_id is None:
        raise HTTPException(status_code=503, detail="Workflow queue unavailable — check Redis connectivity")

    return {"status": "queued", "job_id": job_id, "thread_id": thread_id, "persona_id": persona_id}


@router.get("/workflow/{persona_id}/jobs/{job_id}")
async def get_workflow_job(persona_id: str, job_id: str, request: Request) -> Dict[str, Any]:
    _get_orchestrator(request, persona_id)  # 404s early if the persona doesn't exist
    job = await queue_manager.get_job(reddit_workflow_worker.QUEUE_NAME, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Unknown job '{job_id}'")
    return job


# --- Approvals ------------------------------------------------------

@router.get("/workflow/{persona_id}/approvals")
async def list_pending_approvals(persona_id: str, request: Request) -> Dict[str, Any]:
    factory = _get_factory(request)
    approvals = await factory.approval_store.get_pending_approvals(persona_id)
    return {"persona_id": persona_id, "pending_approvals": approvals}


@router.post("/workflow/{persona_id}/approvals/{approval_id}/respond")
async def respond_to_approval(persona_id: str, approval_id: str,
                               body: ApprovalResponseRequest, request: Request) -> Dict[str, Any]:
    """NOTE: the thread_id in the request body must be the one returned by
    /start for this run — it's how resume_with_approval() finds the paused
    LangGraph checkpoint. The dashboard should carry it through from the
    original queued-job response to whatever UI presents this approval."""
    if body.decision not in ("approved", "rejected"):
        raise HTTPException(status_code=400, detail="decision must be 'approved' or 'rejected'")

    factory = _get_factory(request)
    orchestrator = _get_orchestrator(request, persona_id)

    await factory.approval_store.update_approval_status(
        approval_id=approval_id, status=body.decision,
        approved_by=body.responded_by, reasoning=body.feedback,
    )
    result = await orchestrator.resume_with_approval(
        thread_id=body.thread_id, approval_status=body.decision, feedback=body.feedback,
    )
    logger.info(f"Approval {approval_id} for persona {persona_id}: {body.decision}")
    return result