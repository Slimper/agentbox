"""SASL XOAUTH2 helpers shared by the IMAP client and the SMTP relay provider."""

from __future__ import annotations

import base64


def xoauth2_string(user: str, access_token: str) -> str:
    return f"user={user}\x01auth=Bearer {access_token}\x01\x01"


def xoauth2_b64(user: str, access_token: str) -> str:
    return base64.b64encode(xoauth2_string(user, access_token).encode()).decode()
