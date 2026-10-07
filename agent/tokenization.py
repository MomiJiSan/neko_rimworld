"""One optional tokenizer for public input and delivery accounting."""

from functools import lru_cache
from typing import Any


@lru_cache(maxsize=1)
def tokenizer() -> Any | None:
    try:
        import tiktoken

        return tiktoken.get_encoding("o200k_base")
    except (ImportError, KeyError, RuntimeError, ValueError):
        return None
