"""System prompt assembly and per-verse message formatting for refine-alignment.

Language configs are registered on import. English ("eng") is always available.
Add other languages by importing their modules and calling register_language().
"""

from .core import (
    LanguagePromptConfig,
    build_batch_message,
    build_system_prompt,
    detect_phenomena,
    format_verse_block,
    get_language_config,
    register_language,
)
from . import eng as _eng  # noqa: F401 — registers English config
from . import por as _por  # noqa: F401 — registers Portuguese config
from . import spa as _spa  # noqa: F401 — registers Spanish (Latin American) config

__all__ = [
    "LanguagePromptConfig",
    "build_batch_message",
    "build_system_prompt",
    "detect_phenomena",
    "format_verse_block",
    "get_language_config",
    "register_language",
]
