"""Concrete model backends.

* :class:`OllamaBackend`
* :class:`OpenAIBackend`, :class:`VLLMBackend`, :class:`DeepSeekBackend`
* :class:`AnthropicBackend`

All backends are thin async wrappers over ``httpx``. They lazy-import
``httpx`` so the core package stays light when no backend is used.
"""

from adk.core.backends.anthropic import AnthropicBackend
from adk.core.backends.ollama import OllamaBackend
from adk.core.backends.openai_compat import (
    DeepSeekBackend,
    OpenAIBackend,
    VLLMBackend,
)

__all__ = [
    "AnthropicBackend",
    "DeepSeekBackend",
    "OllamaBackend",
    "OpenAIBackend",
    "VLLMBackend",
]
