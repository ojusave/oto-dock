"""Phone usage reporting — local cost-tracking for the phone daemon.

The phone daemon's turn classifier (Groq) makes non-streaming calls whose token
spend is otherwise invisible in /admin/usage. The daemon drains the accumulated
usage at each call's teardown and POSTs it here; we record ONE usage_records row
per call (``source_type='turn-classifier'``, ``scope='agent'``, ``user_sub='phone'``)
so it rolls up in the per-agent breakdown — mirroring how phone turns + title
generation already attribute.

Auth = the internal proxy master key (the same Bearer the daemon uses for warmup,
via ``verify_api_key``). The hosted relay bills the classifier independently (×1.25
credit); this records the BASE price locally for display — a separate ledger.
"""

import asyncio
import logging

from fastapi import APIRouter, Header
from pydantic import BaseModel

from api.sessions.sessions import verify_api_key
from core.layers.providers import ProviderUsage, get_adapter
from services.billing import usage_service

logger = logging.getLogger("claude-proxy")
router = APIRouter()

# The turn classifier is fixed to this Groq model (dispatcher builds it with no model
# arg). Used to price the row when the daemon doesn't report a model.
_DEFAULT_MODEL = "openai/gpt-oss-120b"


class TurnClassifierUsage(BaseModel):
    agent: str = ""
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    session_id: str = ""


def _record(agent: str, model: str, in_tok: int, out_tok: int, session_id: str | None) -> dict:
    """Price + persist the row (sync DB work — run off the event loop)."""
    cost = get_adapter("groq").calculate_cost(
        model, ProviderUsage(input_tokens=in_tok, output_tokens=out_tok))
    row_id = usage_service.record_usage(
        user_sub="phone",
        agent=agent,
        scope="agent",
        source_type="turn-classifier",
        source_id=session_id,
        cost_usd=cost,
        input_tokens=in_tok,
        output_tokens=out_tok,
        message_count=0,
        provider="groq",
        model=model,
    )
    return {"recorded": bool(row_id), "cost_usd": cost}


@router.post("/v1/phone/usage/turn-classifier")
async def report_turn_classifier_usage(
    req: TurnClassifierUsage, authorization: str | None = Header(None),
):
    """Record one phone call's turn-classifier token spend (local per-agent display)."""
    verify_api_key(authorization)
    model = req.model or _DEFAULT_MODEL
    in_tok = max(0, int(req.input_tokens))
    out_tok = max(0, int(req.output_tokens))
    if not (in_tok or out_tok):
        return {"recorded": False}
    return await asyncio.to_thread(
        _record, req.agent or "", model, in_tok, out_tok, req.session_id or None)
