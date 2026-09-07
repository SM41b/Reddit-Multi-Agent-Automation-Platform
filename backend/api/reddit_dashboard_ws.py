"""
WebSocket bridge for the Reddit dashboard — new file, no equivalent in the
reference repo.

The existing /ws/agent-events endpoint in main.py subscribes to a Redis
pub/sub channel ("agentflow_events"), which is a different transport from
communication/event_bus.py's in-process EnhancedEventBus. DashboardNotifier
(communication/dashboard_notifier.py) publishes to event_bus's
"dashboard_notifications" topic via event_bus.subscribe_to_topic(), which
only delivers to in-process Python callbacks — it does NOT go through
Redis. So the frontend needs its own websocket endpoint that bridges that
in-process topic out to connected browser clients.

This only works because your orchestrator, queue worker, and FastAPI app
all run in the same Python process (per the reference repo's architecture)
— if you ever split the worker into a separate process, this bridge would
need to move to Redis pub/sub instead, same as the existing endpoint does.
"""
import asyncio
from typing import Set

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from loguru import logger

from communication.event_bus import event_bus, AgentEvent

router = APIRouter()

TOPIC = "dashboard_notifications"
_SUBSCRIBER_AGENT_ID = "reddit_dashboard_ws_bridge"

_active_sockets: Set[WebSocket] = set()


async def _forward_to_sockets(event: AgentEvent) -> None:
    """Registered once with event_bus.subscribe_to_topic(); fans out to
    every currently-connected dashboard websocket."""
    payload = {
        "type": event.content.get("update_type", "status"),
        "data": event.content.get("data", {}),
        "message": event.content.get("message", ""),
        "timestamp": event.timestamp.isoformat(),
    }
    dead = []
    for ws in _active_sockets:
        try:
            await ws.send_json(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _active_sockets.discard(ws)


def register_dashboard_bridge() -> None:
    """Call once at startup (from reddit_platform_startup.py) — subscribes
    the forwarder to the topic exactly once regardless of how many clients
    connect/disconnect afterward."""
    event_bus.subscribe_to_topic(_SUBSCRIBER_AGENT_ID, TOPIC, _forward_to_sockets)
    logger.info(f"Dashboard websocket bridge subscribed to event_bus topic '{TOPIC}'")


@router.websocket("/ws/reddit-dashboard")
async def reddit_dashboard_ws(websocket: WebSocket):
    await websocket.accept()
    _active_sockets.add(websocket)
    logger.info(f"Dashboard client connected ({len(_active_sockets)} active)")
    try:
        while True:
            # No inbound messages expected from the client; just keep the
            # connection alive and detect disconnects.
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.error(f"Dashboard websocket error: {e}")
    finally:
        _active_sockets.discard(websocket)
        logger.info(f"Dashboard client disconnected ({len(_active_sockets)} active)")