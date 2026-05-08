"""Mail routes — read endpoints over the signed-in user's mailbox.

Day-one surface is one route: ``GET /api/inbox`` returns the user's
most recent 25 messages. Phase 6 will fan this out into the inbox
list page in the React frontend; for now it's a smoke endpoint so a
real principal can sign in via the OAuth flow and immediately see
their own inbox come through the v3 stack end-to-end.

The route is intentionally thin: ``current_user`` and ``graph_context``
do all the heavy lifting (auth, firm scope, token refresh). The
handler just calls ``list_inbox`` and returns the result. Any future
filter/sort/paginate parameters belong in ``list_inbox`` so the rest
of the orchestrator can use them too.
"""
from fastapi import APIRouter, Depends, Query

from coworker.graph.context import GraphContext, graph_context
from coworker.graph.mail import InboxMessage, list_inbox

router = APIRouter(prefix="/api", tags=["mail"])


@router.get("/inbox")
async def get_inbox(
    top: int = Query(25, ge=1, le=1000),
    ctx: GraphContext = Depends(graph_context),
) -> list[InboxMessage]:
    """Return the most recent ``top`` inbox messages for the signed-in user.

    The 401 / 5xx error responses are produced upstream:
    ``current_user`` raises a generic 401 on any auth failure;
    ``ConnectorAuthError`` from a failed token refresh propagates and
    surfaces as 500 (Phase 12 will install a custom handler that
    converts it to 401 with a sign-in-again hint);
    ``ConnectorRateLimited`` and ``ConnectorTransient`` from
    ``list_inbox`` likewise propagate.
    """
    return await list_inbox(ctx, top=top)
