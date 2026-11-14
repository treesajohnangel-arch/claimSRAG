"""A limit on request body size (D-035), so an upload cannot exhaust the server's memory.

FastAPI reads a request body completely before the route (and before authentication) runs, and the PDF upload
takes its file as the body. This ASGI middleware receives the body first, stopping at ``MAX_UPLOAD_BYTES``:
- **over the limit:** it answers ``413 {"detail": "The request is too large"}`` itself, and the application never
  runs;
- **otherwise:** it replays the body to the application unchanged. FastAPI would have held it in memory anyway.

It looks only at the body's length, never at its content or any header, so it reads no client identity or tenant
data. The limit is generous for policy PDFs (25 MiB by default); every JSON body is far smaller.
"""

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

REQUEST_TOO_LARGE = "The request is too large"

Message = dict[str, Any]


class RequestSizeLimit:
    def __init__(self, app: Any, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        chunks: list[bytes] = []
        size, more = 0, True
        while more:
            message: Message = await receive()
            if message["type"] != "http.request":  # the client went away before sending the whole body
                return
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > self.max_bytes:
                logger.warning("request refused: body over the limit method=%s path=%r max_bytes=%d",
                               scope.get("method"), scope.get("path"), self.max_bytes)
                await self._too_large(send)
                return
            chunks.append(chunk)
            more = message.get("more_body", False)
        body, replayed = b"".join(chunks), False

        async def replay() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()  # after the body: wait for the disconnect, as the server would

        await self.app(scope, replay, send)

    @staticmethod
    async def _too_large(send: Any) -> None:
        content = json.dumps({"detail": REQUEST_TOO_LARGE}).encode()
        await send({"type": "http.response.start", "status": 413,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(content)).encode()),
                                (b"connection", b"close")]})
        await send({"type": "http.response.body", "body": content})
