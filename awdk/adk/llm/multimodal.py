"""Multimodal message helpers for the OpenAI-compatible provider family
(gateway / vLLM / OpenAI).

adk's :class:`~adk.llm.base.Message` carries ``content`` that is normally a
string but may be an OpenAI-style *content-part list* for image input. These
helpers build that list so callers never hand-assemble the shape (and so the
encoding of local image bytes into a ``data:`` URI is done once, correctly).

Only the OpenAI-compatible path (``adk/llm/openai_compat.py``) forwards the
content-part list verbatim to the HTTP body; the Anthropic/Gemini providers use
their own ``content_blocks`` mechanism, so route image requests to a
gateway/vLLM/OpenAI model (e.g. gemma4-12b via the gateway).
"""
from __future__ import annotations

import base64
import mimetypes
from pathlib import Path
from typing import Union

from .base import Message

ImageSource = Union[str, bytes, Path]


def _looks_like_url(s: str) -> bool:
    return s.startswith(("http://", "https://", "data:"))


def to_image_url(image: ImageSource, mime: str | None = None) -> str:
    """Normalize an image into a URL usable as an OpenAI ``image_url``.

    - ``str`` that is already an http(s):// or data: URL -> returned as-is.
    - ``str`` path / :class:`Path` -> read bytes, base64 into a ``data:`` URI
      (mime sniffed from the extension unless ``mime`` is given).
    - ``bytes`` -> base64 into a ``data:`` URI (mime defaults to image/png).
    """
    if isinstance(image, str) and _looks_like_url(image):
        return image
    if isinstance(image, (str, Path)):
        p = Path(image)
        data = p.read_bytes()
        mime = mime or mimetypes.guess_type(str(p))[0] or "image/png"
    elif isinstance(image, bytes):
        data = image
        mime = mime or "image/png"
    else:  # pragma: no cover - defensive
        raise TypeError(f"unsupported image source: {type(image).__name__}")
    b64 = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{b64}"


def image_content_parts(text: str, images: ImageSource | list[ImageSource],
                        detail: str | None = None) -> list[dict]:
    """Build the OpenAI content-part list: the text part first, then one
    ``image_url`` part per image."""
    if not isinstance(images, list):
        images = [images]
    parts: list[dict] = [{"type": "text", "text": text}]
    for img in images:
        img_url: dict = {"url": to_image_url(img)}
        if detail:
            img_url["detail"] = detail
        parts.append({"type": "image_url", "image_url": img_url})
    return parts


def image_message(text: str, images: ImageSource | list[ImageSource],
                  role: str = "user", detail: str | None = None) -> Message:
    """Build a multimodal :class:`Message` (text + one or more images).

    Example::

        from adk.llm.multimodal import image_message
        msg = image_message("What is in this grid?", "grid.png")
        resp = await provider.chat([msg], model="gemma4-12b")
    """
    return Message(role=role, content=image_content_parts(text, images, detail=detail))


__all__ = ["image_message", "image_content_parts", "to_image_url"]
