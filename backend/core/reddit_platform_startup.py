"""
Startup wiring for the Reddit orchestrator stack. Call init_reddit_platform()
from your existing main.py's existing lifespan() function (see the
inline comments in main.py where these calls were added) — this
deliberately does NOT replace main.py wholesale, since that file also
wires up auth, CORS, health checks, and other routes you're keeping.
"""
import os
from fastapi import FastAPI
from loguru import logger

from database.postgres_approvals import PostgresApprovalStore
from communication.dashboard_notifier import DashboardNotifier
from agents.behavior_profile_manager import BehaviorProfileManager
from agents.agent_factory import AgentFactory
from task_queue.queue_manager import queue_manager
from task_queue import reddit_workflow_worker
from api.reddit_dashboard_ws import register_dashboard_bridge


async def init_reddit_platform(app: FastAPI) -> None:
    approval_store = PostgresApprovalStore(dsn=os.getenv("DATABASE_URL"))
    await approval_store.connect()

    notifier = DashboardNotifier()

    behavior_mgr = BehaviorProfileManager(redis_url=os.getenv("REDIS_URL", "redis://localhost:6379"))
    await behavior_mgr.connect()

    factory = AgentFactory(
        approval_store=approval_store,
        notifier=notifier,
        behavior_mgr=behavior_mgr,
    )

    # queue_manager.initialize() is idempotent (guarded by is_initialized)
    # and also creates the reference repo's own standard queues
    # (content_tasks, client_tasks, etc.) — harmless to have those exist
    # unused, and safe to call here even if another startup path in
    # main.py also calls it.
    initialized = await queue_manager.initialize()
    if initialized:
        reddit_workflow_worker.set_factory(factory)
        await reddit_workflow_worker.register_reddit_workflow_queue()
    else:
        logger.warning("Queue system unavailable — reddit_workflow_tasks queue not "
                        "registered; /api/workflow/{persona_id}/start will have nothing "
                        "to enqueue to until Redis is reachable")

    register_dashboard_bridge()

    app.state.approval_store = approval_store
    app.state.notifier = notifier
    app.state.behavior_mgr = behavior_mgr
    app.state.agent_factory = factory

    logger.info("Reddit orchestrator platform initialized: approval store, notifier, "
                "behavior manager, agent factory, and workflow queue worker all wired up")


async def shutdown_reddit_platform(app: FastAPI) -> None:
    # NOTE on ordering in main.py's lifespan: this must run BEFORE
    # queue_manager.stop_all() (which the reference repo's own shutdown
    # code already calls on the shared global queue_manager instance) —
    # stop_worker() here just stops our specific worker cleanly first;
    # stop_all() right after handles the underlying Redis connection and
    # any other registered workers.
    await queue_manager.stop_worker(reddit_workflow_worker.QUEUE_NAME)

    factory: AgentFactory = getattr(app.state, "agent_factory", None)
    if factory:
        await factory.shutdown_all()

    behavior_mgr: BehaviorProfileManager = getattr(app.state, "behavior_mgr", None)
    if behavior_mgr:
        await behavior_mgr.close()

    approval_store: PostgresApprovalStore = getattr(app.state, "approval_store", None)
    if approval_store:
        await approval_store.close()

    logger.info("Reddit orchestrator platform shut down cleanly")