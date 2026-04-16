"""Public webhook receive endpoint.

Path: ``/v1/webhooks/{provider_id}/{subscription_id}``

UNAUTHENTICATED at the HTTP layer — auth is provided by the vendor's
signature in the request itself (HMAC-SHA256 typically). The dispatcher
runs the manifest's signature verification against the row's signing
secret before doing anything else.

GET is supported only so MS Graph's ``?validationToken=xyz`` handshake
works (their first call is a GET when creating subscriptions); other
vendors POST.

**Reverse-proxy bypass required**: this path must be excluded from any
OIDC/forward-auth gate (Authentik, Authelia, oauth2-proxy, Cloudflare
Access) the platform sits behind. Same requirement as
``/v1/triggers/{scope}/{owner}/{slug}``.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request, Response

from services.webhooks import webhook_dispatcher

logger = logging.getLogger("claude-proxy.api.webhooks")
router = APIRouter()


@router.post(
    "/v1/webhooks/relay/{provider_id}",
    include_in_schema=False,
)
async def receive_relay_webhook(provider_id: str, request: Request) -> Response:
    """Receive a relay-FORWARDED vendor event (hosted event delivery).

    Declared ABOVE the generic vendor route — both have two path segments
    and Starlette matches in declaration order ('relay' is a reserved
    provider_id in the manifest validator). POST-only: the relay answers
    vendor handshakes (url_verification etc.) upstream. Auth = the relay's
    forward signature over the verbatim body (X-OtoDock-Event-* headers);
    same reverse-proxy forward-auth bypass requirement as the vendor route.
    """
    raw_body = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items()}
    try:
        status, body, response_headers = await webhook_dispatcher.dispatch_relay_webhook(
            provider_id=provider_id,
            raw_body=raw_body,
            headers=headers,
        )
    except Exception:
        logger.exception(
            "relay webhook dispatcher raised unexpectedly for provider=%s",
            provider_id,
        )
        return Response(
            content=b'{"error":"internal_error"}',
            status_code=500,
            media_type="application/json",
        )
    import json
    return Response(
        content=json.dumps(body).encode("utf-8"),
        status_code=status,
        headers=response_headers or {},
    )


@router.api_route(
    "/v1/webhooks/{provider_id}/{subscription_id}",
    methods=["GET", "POST"],
    include_in_schema=False,  # vendor URLs aren't documented for human consumption
)
async def receive_webhook(
    provider_id: str,
    subscription_id: str,
    request: Request,
) -> Response:
    """Receive a webhook from a vendor.

    Returns the dispatcher's response shape:
      * 200 + JSON ``{status, fired, ...}`` on a normal event (even if
        no triggers matched — we want vendors to NOT retry)
      * 200 + handshake-specific body on URL-verification handshakes
      * 401 on signature mismatch
      * 404 when subscription_id is unknown
      * 410 when subscription is disabled / failed
    """
    raw_body = await request.body()
    # Lowercase + simple-string headers for the dispatcher.
    headers = {k.lower(): v for k, v in request.headers.items()}
    query_params = dict(request.query_params)
    try:
        status, body, response_headers = await webhook_dispatcher.dispatch_webhook(
            provider_id=provider_id,
            subscription_id=subscription_id,
            raw_body=raw_body,
            headers=headers,
            query_params=query_params,
            http_method=request.method,
        )
    except Exception:
        logger.exception(
            "webhook dispatcher raised unexpectedly for provider=%s sub=%s",
            provider_id, subscription_id,
        )
        return Response(
            content=b'{"error":"internal_error"}',
            status_code=500,
            media_type="application/json",
        )

    if isinstance(body, (dict, list)):
        import json
        body_bytes = json.dumps(body).encode("utf-8")
    else:
        body_bytes = str(body).encode("utf-8")
    return Response(
        content=body_bytes,
        status_code=status,
        headers=response_headers or {},
    )
