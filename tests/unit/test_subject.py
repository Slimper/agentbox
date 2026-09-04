from agentbox.domain.subject import normalize_subject, strip_reply_prefixes


def test_strips_prefixes_recursively_and_lowercases():
    assert normalize_subject("Re: RE: Fwd: Ответ: Пересылка: Запрос  КП") == "запрос кп"


def test_handles_numbered_prefix_and_empty():
    assert normalize_subject("Re[2]: hello") == "hello"
    assert normalize_subject(None) == ""
    assert normalize_subject("   ") == ""


def test_strip_keeps_case():
    assert strip_reply_prefixes("Re: Hello World") == "Hello World"
    assert strip_reply_prefixes("Hello") == "Hello"
