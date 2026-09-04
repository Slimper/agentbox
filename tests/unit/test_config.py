from agentbox.config import Settings


def test_settings_read_env(monkeypatch):
    monkeypatch.setenv("AGENTBOX_MANAGED_DOMAIN", "mail.example.test")
    monkeypatch.setenv("AGENTBOX_MAX_ATTACHMENT_BYTES", "1024")
    s = Settings(_env_file=None)
    assert s.managed_domain == "mail.example.test"
    assert s.max_attachment_bytes == 1024
