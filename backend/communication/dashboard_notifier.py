"""
Dashboard notifier, implementing the Notifier interface from
hitl_reddit_orchestrator.py.

Wraps the reference repo's communication/event_bus.py as-is — that module
has no Slack/Supabase dependency baked into its core pub/sub logic, only in
a few standalone helper functions at the bottom (send_slack_notification_
via_event_bus and friends), which this file does not use. Your dashboard's
frontend subscribes to the "dashboard" topic via whatever websocket/SSE
bridge you already have reading from event_bus, instead of a Slack webhook.
"""
from datetime import datetime
from typing import Dict, Any

from communication.event_bus import event_bus


class DashboardNotifier:
    """Implements the Notifier interface used by HITLRedditOrchestrator."""

    DASHBOARD_TOPIC = "dashboard_notifications"

    async def notify_approval_needed(self, agent_name: str, approval_id: str, reason: str) -> None:
        await event_bus.broadcast_to_topic(
            from_agent=agent_name,
            topic=self.DASHBOARD_TOPIC,
            update_data={
                "type": "approval_needed",
                "data": {
                    "agent_name": agent_name,
                    "approval_id": approval_id,
                    "reason": reason,
                    "timestamp": datetime.now().isoformat(),
                },
                "message": f"{agent_name} needs approval: {reason}",
            },
        )

    async def notify_completed(self, agent_name: str, data: Dict[str, Any]) -> None:
        await event_bus.broadcast_to_topic(
            from_agent=agent_name,
            topic=self.DASHBOARD_TOPIC,
            update_data={
                "type": "agent_completed",
                "data": data,
                "message": f"{agent_name} completed its task",
            },
        )