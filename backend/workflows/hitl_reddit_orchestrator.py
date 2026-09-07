"""
HITL-Enhanced LangGraph Orchestrator — Reddit Multi-Agent Platform
Adapted from agruai/multiagent-business-automation's hitl_langgraph_orchestrator.py

Core pattern kept: StateGraph -> agent node -> hitl_checkpoint -> approval_gate
(interrupt) -> side_effects_executor -> back to supervisor.

Swapped: 6 business-role agents -> Research/Content/Analysis agents;
Supabase -> generic ApprovalStore interface (implement against Postgres);
Slack notification -> your own event_bus / dashboard push;
side effects -> Reddit API calls instead of Instagram/HubSpot/invoices;
added: behavior-profile / jitter check as an extra HITL trigger, since your
platform's compliance model depends on it and the reference repo has no
equivalent.
"""
from typing import Dict, List, Any, Optional, TypedDict, Annotated
import asyncio
from datetime import datetime
from loguru import logger
import operator

from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver
from langchain_core.messages import AnyMessage, AIMessage
from langgraph.graph.message import add_messages

# --- Pluggable interfaces you implement against your own stack -------------
# Keep these as thin interfaces so the orchestrator doesn't hardcode Postgres
# or your event bus. Swap the concrete implementation without touching the
# graph logic below.

class ApprovalStore:
    """Implement against PostgreSQL. Mirrors supabase_db's approval calls
    from the reference repo but without the Supabase dependency."""
    async def create_approval_request(self, workspace_id: str, request: Dict) -> Dict:
        raise NotImplementedError

    async def log_audit_event(self, workspace_id: str, user_id: str, agent: str,
                               action: str, details: Dict, sensitive: bool = True) -> None:
        raise NotImplementedError


class Notifier:
    """Implement against your dashboard's event bus / websocket layer
    instead of Slack. The reference repo's event_bus module is reusable
    as-is for this if you kept it."""
    async def notify_approval_needed(self, agent_name: str, approval_id: str,
                                      reason: str) -> None:
        raise NotImplementedError

    async def notify_completed(self, agent_name: str, data: Dict) -> None:
        raise NotImplementedError


class RedditClient:
    """Implement against the Reddit API. Only the action surface the agents
    can trigger needs to live here — keep credential handling out of the
    orchestrator."""
    async def submit_post(self, subreddit: str, title: str, body: str) -> Dict:
        raise NotImplementedError

    async def reply_comment(self, comment_id: str, body: str) -> Dict:
        raise NotImplementedError


class BehaviorProfileManager:
    """No equivalent in the reference repo — this is net-new for your
    compliance requirements. Checked at the HITL checkpoint alongside
    confidence score, so jittered/rate-limited actions can force a human
    review even when the agent itself is confident."""
    async def check(self, workspace_id: str, agent_name: str, action_type: str) -> Dict[str, Any]:
        """Return e.g. {'within_limits': bool, 'reason': str}"""
        raise NotImplementedError

    async def record_action(self, workspace_id: str, action_type: str) -> None:
        """Call after a Reddit action actually executes successfully."""
        raise NotImplementedError


# --- State -------------------------------------------------------------

class HITLAgentFlowState(TypedDict):
    messages: Annotated[List[AnyMessage], add_messages]
    task_brief: str                      # e.g. "research + draft a post about X"
    shared_context: Dict[str, Any]
    agent_outputs: Dict[str, Dict]
    workflow_phase: str
    current_agent: str
    iteration_count: int
    confidence_scores: Dict[str, float]

    pending_approvals: Annotated[List[Dict[str, Any]], operator.add]
    approval_checkpoints: Dict[str, str]
    human_feedback: Dict[str, Any]
    requires_approval: bool

    errors: Annotated[List[Dict[str, Any]], operator.add]
    execution_path: Annotated[List[str], operator.add]

    workspace_id: str
    user_id: str
    thread_id: str


class HITLRedditOrchestrator:
    """LangGraph orchestrator with explicit HITL interrupts, for the
    Research -> Content -> Analysis agent pipeline."""

    def __init__(self, agents: Dict[str, Any], workspace_id: str,
                 approval_store: ApprovalStore, notifier: Notifier,
                 reddit_client: RedditClient, behavior_mgr: BehaviorProfileManager):
        self.agents = agents
        self.workspace_id = workspace_id
        self.approval_store = approval_store
        self.notifier = notifier
        self.reddit_client = reddit_client
        self.behavior_mgr = behavior_mgr
        self.memory = MemorySaver()
        self.workflow = self._build_workflow()

        # Which action types each agent can take that require approval,
        # plus the confidence floor below which anything auto-escalates.
        self.hitl_config = {
            "Research": {
                "requires_approval": [],           # research is read-only; rarely needs a human
                "auto_approve_threshold": 0.6,
            },
            "Content": {
                "requires_approval": ["submit_post", "reply_comment"],
                "auto_approve_threshold": 0.85,     # anything Reddit-facing gets a high bar
            },
            "Analysis": {
                "requires_approval": [],
                "auto_approve_threshold": 0.7,
            },
        }

    def _build_workflow(self):
        workflow = StateGraph(HITLAgentFlowState)

        workflow.add_node("supervisor", self._supervisor_node)
        workflow.add_node("research", self._research_node)
        workflow.add_node("content", self._content_node)
        workflow.add_node("analysis", self._analysis_node)

        workflow.add_node("hitl_checkpoint", self._hitl_checkpoint_node)
        workflow.add_node("approval_gate", self._approval_gate_node)
        workflow.add_node("side_effects_executor", self._side_effects_executor_node)
        workflow.add_node("error_handler", self._error_handler_node)

        workflow.set_entry_point("supervisor")

        workflow.add_conditional_edges(
            "supervisor",
            self._route_next,
            {
                "research": "research",
                "content": "content",
                "analysis": "analysis",
                "end": END,
            },
        )

        for agent in ["research", "content", "analysis"]:
            workflow.add_edge(agent, "hitl_checkpoint")

        workflow.add_conditional_edges(
            "hitl_checkpoint",
            self._hitl_routing,
            {
                "needs_approval": "approval_gate",
                "execute_directly": "side_effects_executor",
                "error": "error_handler",
            },
        )

        workflow.add_conditional_edges(
            "approval_gate",
            self._approval_routing,
            {
                "approved": "side_effects_executor",
                "rejected": "supervisor",
                "timeout": "error_handler",
            },
        )

        workflow.add_edge("side_effects_executor", "supervisor")
        workflow.add_edge("error_handler", "supervisor")

        return workflow.compile(
            checkpointer=self.memory,
            interrupt_before=["approval_gate"],   # human decision happens here
            interrupt_after=["side_effects_executor"],
        )

    # --- Routing -----------------------------------------------------

    async def _supervisor_node(self, state: HITLAgentFlowState) -> HITLAgentFlowState:
        state.setdefault("pending_approvals", [])
        state.setdefault("approval_checkpoints", {})
        state.setdefault("human_feedback", {})

        completed = set(state.get("agent_outputs", {}).keys())

        if "research" not in completed:
            next_agent = "research"
        elif "content" not in completed:
            next_agent = "content"
        elif "analysis" not in completed:
            next_agent = "analysis"
        else:
            next_agent = "end"

        state["current_agent"] = next_agent
        state["iteration_count"] = state.get("iteration_count", 0) + 1
        if next_agent != "end":
            state["execution_path"].append(next_agent)
        state["messages"].append(AIMessage(content=f"Routing to {next_agent}"))
        return state

    def _route_next(self, state: HITLAgentFlowState) -> str:
        return state.get("current_agent", "end")

    # --- Agent nodes ---------------------------------------------------

    async def _research_node(self, state: HITLAgentFlowState) -> HITLAgentFlowState:
        return await self._execute_agent("Research", state)

    async def _content_node(self, state: HITLAgentFlowState) -> HITLAgentFlowState:
        return await self._execute_agent("Content", state)

    async def _analysis_node(self, state: HITLAgentFlowState) -> HITLAgentFlowState:
        return await self._execute_agent("Analysis", state)

    async def _execute_agent(self, agent_name: str, state: HITLAgentFlowState) -> HITLAgentFlowState:
        agent = self.agents.get(agent_name)
        if not agent:
            return state
        try:
            task = {
                "id": f"{agent_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
                "context": state.get("shared_context", {}),
                "brief": state.get("task_brief", ""),
                "previous_outputs": state.get("agent_outputs", {}),
            }
            result = await asyncio.wait_for(agent.execute(task), timeout=60.0)
            state["agent_outputs"][agent_name] = result
            state["confidence_scores"][agent_name] = result.get("confidence", 0.7)
            state["shared_context"][f"{agent_name}_output"] = result.get("output", {})
            await self.notifier.notify_completed(agent_name, result)
        except Exception as e:
            state["errors"].append({
                "agent": agent_name, "error": str(e),
                "timestamp": datetime.now().isoformat(),
            })
            logger.error(f"Error in {agent_name} execution: {e}")
        return state

    # --- HITL checkpoint -------------------------------------------------

    async def _hitl_checkpoint_node(self, state: HITLAgentFlowState) -> HITLAgentFlowState:
        agent_name = state.get("current_agent", "")
        agent_output = state.get("agent_outputs", {}).get(agent_name, {})
        confidence = state.get("confidence_scores", {}).get(agent_name, 0.7)
        config = self.hitl_config.get(agent_name, {})

        requires_approval = False
        reason = ""

        if confidence < config.get("auto_approve_threshold", 0.8):
            requires_approval = True
            reason = f"Low confidence ({confidence:.2f})"

        output_data = agent_output.get("output", {})
        action_keys = [k for k in config.get("requires_approval", []) if k in output_data]
        if action_keys:
            requires_approval = True
            reason = f"Reddit-facing action requires approval: {', '.join(action_keys)}"

        # Behavior-profile / jitter check — this is the piece with no
        # equivalent in the reference repo.
        for action_type in action_keys:
            profile_check = await self.behavior_mgr.check(
                state["workspace_id"], agent_name, action_type
            )
            if not profile_check.get("within_limits", True):
                requires_approval = True
                reason = f"Behavior profile flag: {profile_check.get('reason')}"

        state["requires_approval"] = requires_approval

        if requires_approval:
            approval_request = {
                "agent_name": agent_name,
                "action_type": "agent_execution",
                "action_description": reason,
                "thread_id": state["thread_id"],
                "payload": {
                    "agent_output": agent_output,
                    "confidence": confidence,
                    "context": state.get("shared_context", {}),
                },
                "reasoning": reason,
            }
            approval_record = await self.approval_store.create_approval_request(
                state["workspace_id"], approval_request
            )
            state["pending_approvals"].append(approval_record)
            state["approval_checkpoints"][agent_name] = approval_record.get("id", "")
            logger.info(f"HITL checkpoint: {agent_name} requires approval - {reason}")

        state["messages"].append(AIMessage(
            content=f"HITL checkpoint: {agent_name} - "
                    f"{'Requires approval' if requires_approval else 'Auto-approved'}"
        ))
        return state

    async def _approval_gate_node(self, state: HITLAgentFlowState) -> HITLAgentFlowState:
        agent_name = state.get("current_agent", "")
        state["messages"].append(AIMessage(
            content=f"⏸️ Workflow interrupted: waiting for human approval for {agent_name}"
        ))
        approval_id = state.get("approval_checkpoints", {}).get(agent_name, "")
        reason = state.get("pending_approvals", [])[-1].get("reasoning", "") \
            if state.get("pending_approvals") else ""
        await self.notifier.notify_approval_needed(agent_name, approval_id, reason)
        return state

    def _hitl_routing(self, state: HITLAgentFlowState) -> str:
        return "needs_approval" if state.get("requires_approval", False) else "execute_directly"

    def _approval_routing(self, state: HITLAgentFlowState) -> str:
        status = state.get("human_feedback", {}).get("approval_status")
        if status == "approved":
            return "approved"
        elif status == "rejected":
            return "rejected"
        return "timeout"

    # --- Side effects (Reddit actions instead of Instagram/HubSpot/invoices) --

    async def _side_effects_executor_node(self, state: HITLAgentFlowState) -> HITLAgentFlowState:
        agent_name = state.get("current_agent", "")
        agent_output = state.get("agent_outputs", {}).get(agent_name, {})
        output_data = agent_output.get("output", {})

        if agent_name == "Content":
            await self._execute_reddit_side_effects(state["workspace_id"], output_data)

        await self.approval_store.log_audit_event(
            workspace_id=state["workspace_id"],
            user_id=state["user_id"],
            agent=agent_name,
            action="side_effects_executed",
            details={"output": agent_output},
            sensitive=True,
        )
        state["messages"].append(AIMessage(content=f"✅ Side effects executed for {agent_name}"))
        return state

    async def _execute_reddit_side_effects(self, workspace_id: str, output_data: Dict[str, Any]):
        if "submit_post" in output_data:
            p = output_data["submit_post"]
            logger.info(f"Submitting Reddit post to r/{p.get('subreddit')}")
            result = await self.reddit_client.submit_post(p["subreddit"], p["title"], p["body"])
            if result.get("status") == "success":
                await self.behavior_mgr.record_action(workspace_id, "submit_post")
        if "reply_comment" in output_data:
            r = output_data["reply_comment"]
            logger.info(f"Replying to comment {r.get('comment_id')}")
            result = await self.reddit_client.reply_comment(r["comment_id"], r["body"])
            if result.get("status") == "success":
                await self.behavior_mgr.record_action(workspace_id, "reply_comment")

    async def _error_handler_node(self, state: HITLAgentFlowState) -> HITLAgentFlowState:
        errors = state.get("errors", [])
        if errors:
            logger.error(f"HITL error handler: {errors[-1]}")
        return state

    # --- Resume after human decision -----------------------------------

    async def resume_with_approval(self, thread_id: str, approval_status: str,
                                    feedback: str = "") -> Dict[str, Any]:
        config = {"configurable": {"thread_id": thread_id}}
        human_feedback = {
            "approval_status": approval_status,
            "feedback": feedback,
            "timestamp": datetime.now().isoformat(),
        }
        final_state = None
        async for chunk in self.workflow.astream({"human_feedback": human_feedback}, config=config):
            final_state = chunk
        return {"status": "resumed", "approval_status": approval_status, "final_state": final_state}