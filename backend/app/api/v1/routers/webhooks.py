"""``/api/v1/webhooks/providers/{provider}`` HTTP router (Slice α8.3b).

The inbound provider-webhook ingress — a **thin, unauthenticated** endpoint
(authentication is the *signature*, not a bearer token). It reads the **raw**
request body (the signature covers a hash of the exact bytes), hands it to the
:class:`ReceiveProviderWebhook` use case, and maps the outcome to a status code:

* ``401`` — signature missing / malformed / stale / invalid (payload discarded).
* ``400`` — verified but no usable resume coordinate.
* ``404`` — no verifier registered for ``{provider}``.
* ``200`` — accepted (resumed / in-progress / duplicate / unknown job). We ack
  duplicates and unknown job ids so the provider stops retrying.

Per W8.3b.1 the router/use case never mutate workflow state directly; they only
trigger the frozen ``CompletionEngine.complete()`` pipeline.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse

from app.api.v1.deps import ReceiveProviderWebhookDep
from app.api.v1.helpers import envelope
from app.application.interfaces.webhook_verifier import (
    WebhookMalformedError,
    WebhookVerificationError,
)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.post("/providers/{provider}")
async def receive_provider_webhook(
    provider: str,
    request: Request,
    use_case: ReceiveProviderWebhookDep,
) -> JSONResponse:
    """Verify + trigger completion for one inbound provider webhook."""
    body = await request.body()
    # Lower-case header keys so the verifier's lookups are case-insensitive.
    headers = {key.lower(): value for key, value in request.headers.items()}

    try:
        result = await use_case.execute(provider=provider, body=body, headers=headers)
    except WebhookVerificationError:
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={
                "error": {"code": "WEBHOOK_UNVERIFIED", "message": "signature verification failed"}
            },
        )
    except WebhookMalformedError:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "error": {"code": "WEBHOOK_MALFORMED", "message": "webhook missing required data"}
            },
        )

    if result.status == "unsupported":
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={
                "error": {
                    "code": "WEBHOOK_PROVIDER_UNKNOWN",
                    "message": f"no webhook verifier for '{provider}'",
                }
            },
        )

    payload = {
        "status": result.status,
        "workflow_run_id": str(result.workflow_run_id) if result.workflow_run_id else None,
        "run_status": result.run_status,
    }
    return JSONResponse(status_code=status.HTTP_200_OK, content=envelope(payload, request))
