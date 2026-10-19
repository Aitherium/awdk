"""Concrete model backends.

* :class:`OpenAIBackend`, :class:`VLLMBackend`, :class:`DeepSeekBackend`
* :class:`LlamaCppBackend` (local llama-server)
* :class:`AnthropicBackend`

All backends are thin async wrappers over ``httpx``. They lazy-import
``httpx`` so the core package stays light when no backend is used.
"""

from adk.core.backends.anthropic import AnthropicBackend
from adk.core.backends.openai_compat import (
    DeepSeekBackend,
    LlamaCppBackend,
    OpenAIBackend,
    VLLMBackend,
)

__all__ = [
    "AnthropicBackend",
    "DeepSeekBackend",
    "LlamaCppBackend",
    "OpenAIBackend",
    "VLLMBackend",
]
