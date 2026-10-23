from __future__ import annotations

import base64
import hashlib
from urllib.parse import quote


def yun139_signature(body: str, timestamp: str, nonce: str) -> str:
    """Sign the plaintext JSON, before the login API's transport encryption."""
    encoded = "".join(sorted(quote(body, safe="~()*!.'-_")))
    body_hash = hashlib.md5(base64.b64encode(encoded.encode())).hexdigest()
    time_hash = hashlib.md5(f"{timestamp}:{nonce}".encode()).hexdigest()
    return hashlib.md5(f"{body_hash}{time_hash}".encode()).hexdigest().upper()
