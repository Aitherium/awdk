"""The OpenAI-shaped image lane, exercised against a real HTTP server.

WHY THIS EXISTS

`adk.images` ships two generate paths. The ComfyUI one is verified live on this
hardware every time anyone runs `adk image`. The OpenAI one -- used for Sana,
SD.Next and anything else that adopted that protocol -- had **never executed**,
because none of those is running here (`aither-sana.service` is masked). A code
path that has never run is not a feature; it is a claim, and it shipped inside a
public skill telling strangers those lanes work.

So this drives `_openai_generate` over a real socket, and asserts the two
directions that matter:

  * a well-formed response yields the image bytes;
  * a **200 with no image** RAISES rather than quietly returning nothing.

That second one is the whole point. A fail-closed path that always returns empty
passes every "returns nothing" assertion trivially, and an image that silently
did not arrive looks exactly like a model with no ideas. The failure has to be
loud, and nothing but a positive-and-negative pair proves it.

The server here is a test double for the PROTOCOL, not a stand-in for verifying
the product: the product is verified against the real backend by `adk image`.
"""

from __future__ import annotations

import asyncio
import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from adk.images import ImageError, ImageRequest, Lane, _openai_generate

# A 1x1 red PNG -- small enough to keep the test instant, real enough that a
# caller decoding it gets actual image bytes.
RED_DOT = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmM"
    "IQAAAABJRU5ErkJggg=="
)


class _Handler(BaseHTTPRequestHandler):
    mode = "ok"          # set per-test on the class
    seen: dict = {}

    def log_message(self, *a):  # keep pytest output clean
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        type(self).seen = body

        if type(self).mode == "empty":
            payload, code = {"data": []}, 200
        elif type(self).mode == "refuse":
            payload, code = {"error": "no model loaded"}, 500
        else:
            payload, code = {"data": [{"b64_json": base64.b64encode(RED_DOT).decode()}]}, 200

        raw = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


@pytest.fixture()
def server():
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()


def _lane(port: int) -> Lane:
    return Lane("sana", "Sana", port, "openai", True, 200, "ready (HTTP 200)")


def test_openai_lane_returns_real_image_bytes(server):
    _Handler.mode = "ok"
    out = asyncio.run(_openai_generate(
        _lane(server.server_port),
        ImageRequest(prompt="a lighthouse in fog", width=512, height=512),
    ))
    assert out["backend"] == "sana"
    assert len(out["images_b64"]) == 1
    # Decodes to a real PNG -- not merely a non-empty string.
    assert base64.b64decode(out["images_b64"][0])[:4] == b"\x89PNG"


def test_openai_lane_sends_the_shape_the_protocol_expects(server):
    _Handler.mode = "ok"
    asyncio.run(_openai_generate(
        _lane(server.server_port),
        ImageRequest(prompt="x", negative="blurry", width=640, height=480),
    ))
    sent = _Handler.seen
    # size is one string, not two ints -- getting this wrong is a 422 the caller
    # never reads, and it is invisible until a real backend refuses it.
    assert sent["size"] == "640x480"
    assert sent["prompt"] == "x"
    assert sent["negative_prompt"] == "blurry"
    assert sent["response_format"] == "b64_json"


def test_a_200_with_no_image_raises_rather_than_returning_nothing(server):
    """The silent no-op guard. THIS is the assertion that earns the test."""
    _Handler.mode = "empty"
    with pytest.raises(ImageError) as e:
        asyncio.run(_openai_generate(
            _lane(server.server_port), ImageRequest(prompt="x"),
        ))
    msg = str(e.value)
    # The message must be actionable: name the lane and say what to check.
    assert "Sana" in msg
    assert "no model loaded" in msg or "no image" in msg


def test_a_non_200_names_the_lane_and_the_status(server):
    _Handler.mode = "refuse"
    with pytest.raises(ImageError) as e:
        asyncio.run(_openai_generate(
            _lane(server.server_port), ImageRequest(prompt="x"),
        ))
    msg = str(e.value)
    assert "Sana" in msg and "500" in msg


def test_an_unreachable_lane_does_not_hang_forever():
    """A closed port must fail, not wedge. Port 1 is reliably refused."""
    with pytest.raises(Exception):
        asyncio.run(_openai_generate(
            _lane(1), ImageRequest(prompt="x"),
        ))
