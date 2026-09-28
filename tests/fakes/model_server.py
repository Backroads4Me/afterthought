"""The fake OpenAI-compatible endpoint every model test runs against.

It serves the one route ``aft.model`` calls, ``POST /v1/chat/completions``, and answers
from a script of replies, so a test decides what the model says, whether it says it
twice, whether the endpoint answers at all, and how long it takes. It listens on
127.0.0.1 on a port the OS picked, so nothing reaches the network or depends on a real
endpoint being up.

    from fakes.model_server import FakeEndpoint, Reply

    with FakeEndpoint([{"answer": "yes"}, Reply(status=500)]) as fake:
        # the first call gets {"answer": "yes"}; a second call would get the 500
        bodies = fake.requests          # parsed JSON bodies, in the order received

Replies are used in order and the last one repeats once the script runs out, so a test
that only cares about the first two calls does not have to script the rest. A plain
value is the assistant message content of a 200, JSON-encoded unless it is already a
string; ``Reply`` covers the rest: ``status`` for an error response, ``delay`` to hold
the connection open before answering, ``usage`` to report ``(prompt, completion)``
token counts (or ``usage=None`` to report none at all), and ``raw`` to answer with
bytes exactly as given, for an endpoint that does not speak HTTP properly.

Point a config at it by naming the endpoint in ``config.toml``, then load it as usual:

    (home / "config.toml").write_text(
        f'vault = "{vault}"\\nhost = "testhost"\\n'
        f'[model]\\nendpoint = "{fake.endpoint}"\\nname = "fake-model"\\n'
    )
    cfg = configmod.load()
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

MODEL_NAME = "fake-model"
CHAT_COMPLETIONS = "/v1/chat/completions"


@dataclass
class Reply:
    """One scripted answer from the endpoint."""

    status: int = 200
    content: Any = None
    delay: float = 0.0
    usage: tuple[int, int] | None = (0, 0)
    raw: bytes | None = None


class FakeEndpoint:
    """A chat completions endpoint driven by a script of replies."""

    def __init__(self, replies: Iterable[Any] = ()) -> None:
        self.script = [
            reply if isinstance(reply, Reply) else Reply(content=reply) for reply in replies
        ]
        self.requests: list[dict] = []
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def __enter__(self) -> FakeEndpoint:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object,
    ) -> None:
        self.stop()

    @property
    def endpoint(self) -> str:
        """The base URL a config points at, in the form the setting takes."""
        port = self._server.server_address[1] if self._server else 0
        return f"http://127.0.0.1:{port}/v1"

    def start(self) -> FakeEndpoint:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: object) -> None:
                """Stay off stderr: a test that scripts failures is not a failure."""

            def do_POST(self) -> None:
                if self.path != CHAT_COMPLETIONS:
                    _send(self, HTTPStatus.NOT_FOUND, {"error": {"message": self.path}})
                    return
                body = self.rfile.read(_length(self))
                with fake._lock:
                    fake.requests.append(_json_or_empty(body))
                    call = len(fake.requests) - 1
                reply = fake.script[min(call, len(fake.script) - 1)] if fake.script else Reply()
                if reply.delay:
                    time.sleep(reply.delay)
                if reply.raw is not None:
                    _send_raw(self, reply.raw)
                    return
                _send(self, reply.status, _payload(call, reply))

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._server = None
        self._thread = None


def _length(handler: BaseHTTPRequestHandler) -> int:
    try:
        return int(handler.headers.get("Content-Length") or 0)
    except ValueError:
        return 0


def _json_or_empty(body: bytes) -> dict:
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _payload(call: int, reply: Reply) -> dict:
    if reply.status >= 400:
        return {"error": {"message": f"fake endpoint refused call {call} with {reply.status}"}}
    content = reply.content if isinstance(reply.content, str) else json.dumps(reply.content)
    payload = {
        "id": f"chatcmpl-fake-{call}",
        "object": "chat.completion",
        "model": MODEL_NAME,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
    }
    if reply.usage is not None:
        prompt, completion = reply.usage
        payload["usage"] = {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
        }
    return payload


def _send(handler: BaseHTTPRequestHandler, status: int, payload: dict) -> None:
    body = json.dumps(payload).encode()
    try:
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)
    except OSError:
        # The client gave up and closed the connection, which is the point of a test
        # that scripts a delay.
        pass


def _send_raw(handler: BaseHTTPRequestHandler, raw: bytes) -> None:
    """Answer with these bytes exactly, then hang up, as a broken endpoint would."""
    handler.close_connection = True
    try:
        handler.wfile.write(raw)
    except OSError:
        # The client gave up and closed the connection first.
        pass
