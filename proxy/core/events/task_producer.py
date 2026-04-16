"""Task producer — feeds CommonEvent objects to a ChatStreamPump for task execution.

Handles the settle + background agent nudge flow within the pump's event stream.
All events go through the pump → saved as chat_messages → rich structured output.

Also broadcasts a simplified per-event SSE stream consumed by the schedules-mcp's
``run_task(wait=true)`` (``GET /v1/tasks/runs/{run_id}/stream``), so MCPs that
spawn tasks can stream live output back to the calling agent without
re-implementing chat-message decoding.
"""

import asyncio
import logging

from core.events.common_events import (
    CommonEvent, SUBAGENT_START, BG_COMMAND_START, TEXT, TOOL_USE, TOOL_RESULT,
    DONE, ERROR, QUEUE_TURN, PRODUCER_DONE,
)
from core.execution_layer import ExecutionLayer
from core.session.session_state import get_subagent_registry
from core.events.bg_command_state import get_bg_command_registry

logger = logging.getLogger("claude-proxy")


# ---------------------------------------------------------------------------
# SSE broadcast bridge for the schedules-mcp ``/v1/tasks/runs/{run_id}/stream``
# endpoint. Producers populate ``_run_subscribers`` queues so each event is
# fanned out to the MCP-side stream consumer in addition to the chat pump.
# ---------------------------------------------------------------------------

def _event_to_sse(event: CommonEvent) -> dict | None:
    """Convert a CommonEvent to SSE broadcast format for _run_subscribers."""
    if event.type == TEXT:
        content = event.data.get("content", "")
        if content:
            return {"type": "text", "text": content}

    elif event.type == TOOL_USE:
        return {
            "type": "tool_start",
            "name": event.data.get("name", ""),
            "summary": "",
        }

    elif event.type == TOOL_RESULT:
        return {
            "type": "tool_end",
            "name": event.data.get("name", ""),
        }

    elif event.type == SUBAGENT_START:
        return {
            "type": "task_spawn",
            "subagent_type": event.data.get("subagent_type", ""),
        }

    elif event.type == DONE:
        # Don't emit SSE done here — the producer sends it explicitly at the end
        return None

    return None


# ---------------------------------------------------------------------------
# Task producer coroutine
# ---------------------------------------------------------------------------

async def task_produce(
    layer: ExecutionLayer,
    session_id: str,
    prompt: str,
    event_queue: asyncio.Queue,
    run_id: str,
    broadcast_fn=None,
    settle_timeout: float = 30.0,
) -> None:
    """Producer for task execution — sends prompt through ExecutionLayer, routes to pump.

    After the main turn, checks for background agents and sends nudge if needed.
    Broadcasts events to SSE subscribers for the schedules-mcp stream consumer.

    Args:
        layer: ExecutionLayer for the agent.
        session_id: Session ID.
        prompt: Task prompt text.
        event_queue: Queue for CommonEvent objects → ChatStreamPump reads these.
        run_id: Task run ID (for SSE broadcast).
        broadcast_fn: Optional async fn(run_id, event_dict) for SSE broadcast.
        settle_timeout: Seconds to wait for background agents in settle mode.
    """
    bg_count = 0
    bgcmd_count = 0

    async def _broadcast(event: CommonEvent):
        """Forward event to SSE subscribers if broadcast_fn provided."""
        if broadcast_fn is None:
            return
        sse_evt = _event_to_sse(event)
        if sse_evt:
            try:
                await broadcast_fn(run_id, sse_evt)
            except Exception:
                pass

    try:
        async with layer.session_lock(session_id):
            # Main turn with settle (waits for background agents at CLI level)
            async for event in layer.send_message(
                session_id, prompt, settle_after_result=settle_timeout,
            ):
                await event_queue.put(event)
                await _broadcast(event)

                # Count background work (for the nudge wording + cohort). Their
                # completion is tracked deterministically in the registries
                # (subagents via SubagentStop hooks; bash commands via the
                # task_updated frame the settle loop drains) — not by counting
                # notifications here.
                if (event.type == SUBAGENT_START
                        and event.data.get("run_in_background")):
                    bg_count += 1
                elif event.type == BG_COMMAND_START:
                    bgcmd_count += 1

            # Background work — the delegation contract REQUIRES a delegated
            # agent's result return only after its bg sub-agents AND bg shell
            # commands finished AND it synthesized. Layer timings, one uniform
            # handling:
            #   CLI   — settle already drained both before send_message returned
            #           (subagents via SubagentStop; commands via task_updated on
            #           stdout), so they show up only as the *_count tallies and
            #           both registries read 0 here.
            #   Codex — the main turn ends while bg subs keep running on their own
            #           threads, so they're still pending in the subagent registry
            #           here (Codex has no background bash).
            # Wait for whichever cohort is outstanding (layer-agnostic, via the
            # registries), then nudge to review + synthesize. Loop so a synthesis
            # turn that itself spawns MORE bg work is also awaited — never
            # returns B's result with bg work still outstanding.
            reg = get_subagent_registry(session_id)
            bgreg = get_bg_command_registry(session_id)
            # Subagents keep the spawn-tally contract (any bg spawn forces a
            # review turn — Codex subs are genuinely pending here, and a CLI
            # sub's report may postdate the model's final text). Bash commands
            # are exempt when every completion was already surfaced to the
            # model DURING generation (the CLI injects the task-notification
            # into the live turn): only pending or UNSURFACED completions
            # (settle-phase / post-turn resolves) force the review turn.
            cohort = (
                (bg_count or reg.pending_count)
                + bgreg.pending_count + bgreg.unsurfaced_count
            )
            while cohort > 0:
                if reg.pending_count > 0:
                    logger.info(
                        f"Task {run_id[:8]}: {reg.pending_count} bg agent(s) "
                        f"pending, waiting up to 120s"
                    )
                    await layer.wait_for_bg_subagents(session_id, timeout=120.0)
                if bgreg.pending_count > 0:
                    logger.info(
                        f"Task {run_id[:8]}: {bgreg.pending_count} bg command(s) "
                        f"pending, waiting up to 120s"
                    )
                    await layer.wait_for_bg_commands(session_id, timeout=120.0)

                bits = []
                if bg_count:
                    bits.append(f"{bg_count} background agent(s)")
                # Mention commands only when bash actually participates in
                # this nudge — never the ones the model already reviewed
                # inline during the turn.
                bash_unseen = bgreg.pending_count + bgreg.unsurfaced_count
                if bash_unseen:
                    bits.append(f"{bash_unseen} background command(s)")
                what = " and ".join(bits) or "background work"
                nudge = (
                    f"Your {what} have completed their work. "
                    f"Please review the output and continue with the task."
                )
                logger.info(f"Task {run_id[:8]}: sending bg completion nudge ({what})")
                # The review turn we are about to send surfaces them.
                bgreg.clear_unsurfaced()

                # Emit as queued user message so pump shows it as a turn boundary
                await event_queue.put(
                    CommonEvent(type=QUEUE_TURN, data={"text": nudge})
                )

                async for event in layer.send_message(session_id, nudge):
                    await event_queue.put(event)
                    await _broadcast(event)
                    # A synthesis turn can spawn MORE bg work — keep tallies fresh
                    # so the loop waits for it too.
                    if (event.type == SUBAGENT_START
                            and event.data.get("run_in_background")):
                        bg_count += 1
                    elif event.type == BG_COMMAND_START:
                        bgcmd_count += 1

                # Re-check both registries; loop until a turn leaves none
                # pending and no completion landed unseen after its final text.
                cohort = (
                    reg.pending_count
                    + bgreg.pending_count + bgreg.unsurfaced_count
                )

        # Broadcast SSE done
        if broadcast_fn:
            try:
                await broadcast_fn(run_id, {"type": "done", "status": "completed"})
            except Exception:
                pass

    except asyncio.CancelledError:
        # Task was cancelled
        if broadcast_fn:
            try:
                await broadcast_fn(run_id, {"type": "done", "status": "cancelled"})
            except Exception:
                pass
        raise

    except Exception as e:
        logger.error(f"Task producer error: {e}", exc_info=True)
        await event_queue.put(CommonEvent(type=ERROR, data={"message": str(e)}))
        if broadcast_fn:
            try:
                await broadcast_fn(run_id, {"type": "done", "status": "failed"})
            except Exception:
                pass

    finally:
        await event_queue.put(CommonEvent(type=PRODUCER_DONE, data={}))
