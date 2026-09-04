from agentbox.mime.dsn import classify_dsn, parse_dsn

DSN = b"""From: MAILER-DAEMON@relay.example
To: bounce+01J@agentbox.local
Subject: Undelivered Mail Returned to Sender
Content-Type: multipart/report; report-type=delivery-status; boundary="B"
MIME-Version: 1.0

--B
Content-Type: text/plain

This is the mail system.
--B
Content-Type: message/delivery-status

Reporting-MTA: dns; relay.example

Final-Recipient: rfc822; nobody@supplier.ru
Action: failed
Status: 5.1.1
Diagnostic-Code: smtp; 550 5.1.1 User unknown

--B
Content-Type: message/rfc822

From: agent@agentbox.local
Subject: original

hi
--B--
"""


def test_parse_and_classify_failed():
    rs = parse_dsn(DSN)
    assert len(rs) == 1
    assert rs[0].recipient == "nobody@supplier.ru" and rs[0].action == "failed" and rs[0].status == "5.1.1"
    assert "User unknown" in rs[0].diagnostic
    assert classify_dsn(rs) == "bounced"


def test_classify_delayed_and_unknown():
    delayed = DSN.replace(b"Action: failed", b"Action: delayed").replace(b"Status: 5.1.1", b"Status: 4.4.1")
    assert classify_dsn(parse_dsn(delayed)) == "deferred"
    assert classify_dsn(parse_dsn(b"Subject: x\r\n\r\nnot a dsn")) == "unknown"
