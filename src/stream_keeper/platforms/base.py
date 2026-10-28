from __future__ import annotations

import re
from collections.abc import Iterator

_URL_CANDIDATE_PATTERN = re.compile(
    r"https?://[A-Z0-9._~:/?#\[\]@!$&()*+,;=%-]+",
    re.IGNORECASE,
)
_TRAILING_URL_PUNCTUATION = ".,;:!?)]}，。；：！？、）》】」』"


def iter_url_candidates(value: str) -> Iterator[str]:
    """Yield HTTP URLs embedded in a pasted URL or platform share message."""

    for match in _URL_CANDIDATE_PATTERN.finditer(value.strip()):
        candidate = match.group(0).rstrip(_TRAILING_URL_PUNCTUATION)
        if candidate:
            yield candidate
