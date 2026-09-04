"""SMTP XOAUTH2 path of the relay provider against an in-process aiosmtpd server."""

import base64
import socket

import pytest
from aiosmtpd.controller import Controller
from aiosmtpd.smtp import SMTP as SMTPServer
from aiosmtpd.smtp import AuthResult

from agentbox.connectors.xoauth2 import xoauth2_b64, xoauth2_string
from agentbox.providers.base import Envelope
from agentbox.providers.smtp_relay import SMTPRelayProvider


class Handler:
    def __init__(self):
        self.messages = []

    async def handle_DATA(self, server, session, envelope):
        self.messages.append((envelope.mail_from, envelope.rcpt_tos, envelope.content))
        return "250 OK queued as xo1"


class Auth:
    def __init__(self):
        self.seen = []

    def __call__(self, server, session, envelope, mechanism, auth_data):
        self.seen.append((mechanism, auth_data))
        return AuthResult(success=mechanism == "XOAUTH2" and b"auth=Bearer tok-123" in auth_data)


class XOAuthSMTP(SMTPServer):
    async def auth_XOAUTH2(self, server, args):
        blob = base64.b64decode(args[1]) if len(args) > 1 else b""
        return self._authenticate("XOAUTH2", blob)


def test_xoauth2_string():
    assert xoauth2_string("a@b.c", "t") == "user=a@b.c\x01auth=Bearer t\x01\x01"
    assert base64.b64decode(xoauth2_b64("a@b.c", "t")).decode() == "user=a@b.c\x01auth=Bearer t\x01\x01"


@pytest.mark.asyncio
async def test_relay_sends_with_xoauth2():
    handler, auth = Handler(), Auth()

    class Ctl(Controller):
        def factory(self):
            return XOAuthSMTP(self.handler, authenticator=auth, auth_require_tls=False, auth_required=True)

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    ctl = Ctl(handler, hostname="127.0.0.1", port=port)
    ctl.start()
    try:
        provider = SMTPRelayProvider(host="127.0.0.1", port=ctl.port, username="robot@corp.ru", oauth_token="tok-123")
        raw = b"From: robot@corp.ru\r\nTo: x@example.com\r\nSubject: hi\r\n\r\nbody\r\n"
        result = await provider.send(Envelope(mail_from="robot@corp.ru", rcpt_to=["x@example.com"], message_id="msg_test"), None, raw)
        assert result.accepted and result.provider_message_id == "xo1"
        assert handler.messages[0][0] == "robot@corp.ru"
        assert auth.seen[0][0] == "XOAUTH2" and auth.seen[0][1] == b"user=robot@corp.ru\x01auth=Bearer tok-123\x01\x01"
    finally:
        ctl.stop()
