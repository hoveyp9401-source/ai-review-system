from __future__ import annotations

import logging

from app.api import webhook


def test_callback_log_metadata_never_contains_credentials_or_business_text(
    caplog,
) -> None:
    signature = "secret-signature-value"
    nonce = "secret-nonce-value"
    token = "secret-token-value"
    business_text = "Confidential case progress and daily report body"

    with caplog.at_level(logging.INFO, logger=webhook.__name__):
        webhook._log_callback_metadata(
            "test_callback",
            {
                "msg_signature": signature,
                "nonce": nonce,
                "token": token,
            },
            payload=business_text,
        )

    rendered = caplog.text
    assert "test_callback" in rendered
    assert "payload_chars=" in rendered
    assert "payload_sha256=" in rendered
    assert signature not in rendered
    assert nonce not in rendered
    assert token not in rendered
    assert business_text not in rendered


def test_webhook_runtime_has_no_direct_stdout_logging_of_callback_data() -> None:
    source = webhook.__file__
    assert source is not None
    text = open(source, encoding="utf-8").read()

    assert "print(" not in text
    assert "raw[:1000]" not in text
    assert "DingTalk POST params:" not in text
    assert "DingTalk event subscription payload:" not in text
