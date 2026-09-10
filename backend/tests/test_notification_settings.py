from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.api.settings import (
    CustomWebhookPrefsIn,
    EmailSmtpPrefsIn,
    update_custom_webhook,
    update_email_smtp,
)


def test_custom_webhook_settings_validate_url_and_store_secret(monkeypatch):
    saved = {}
    monkeypatch.setattr(
        "app.services.preferences.set_custom_webhook_url",
        lambda url: saved.update(url=url) or url,
    )
    monkeypatch.setattr(
        "app.secrets_store.set_custom_webhook_secret",
        lambda secret: saved.update(secret=secret) or secret,
    )
    monkeypatch.setattr(
        "app.secrets_store.get_custom_webhook_secret",
        lambda: saved.get("secret", ""),
    )

    result = update_custom_webhook(CustomWebhookPrefsIn(
        url="https://hooks.example.com/tickflow",
        secret="shared-secret",
    ))

    assert saved == {
        "url": "https://hooks.example.com/tickflow",
        "secret": "shared-secret",
    }
    assert result["custom_webhook_secret_set"] is True

    with pytest.raises(HTTPException, match=r"HTTP\(S\)"):
        update_custom_webhook(CustomWebhookPrefsIn(url="file:///tmp/hook"))


def test_email_settings_validate_and_store_password_separately(monkeypatch):
    saved = {}
    monkeypatch.setattr(
        "app.services.preferences.set_email_smtp_config",
        lambda config: saved.update(config=config) or config,
    )
    monkeypatch.setattr(
        "app.secrets_store.set_email_smtp_password",
        lambda password: saved.update(password=password) or password,
    )
    monkeypatch.setattr(
        "app.secrets_store.get_email_smtp_password",
        lambda: saved.get("password", ""),
    )

    result = update_email_smtp(EmailSmtpPrefsIn(
        host="smtp.example.com",
        port=587,
        security="starttls",
        username="bot@example.com",
        password="smtp-password",
        from_address="bot@example.com",
        to_addresses=["alerts@example.com", "alerts@example.com"],
    ))

    assert saved["password"] == "smtp-password"
    assert saved["config"]["to_addresses"] == ["alerts@example.com"]
    assert result["email_smtp_password_set"] is True

    with pytest.raises(HTTPException, match="收件人"):
        update_email_smtp(EmailSmtpPrefsIn(
            host="smtp.example.com",
            from_address="bot@example.com",
            to_addresses=["not-an-email"],
        ))

    saved.clear()
    with pytest.raises(HTTPException, match="密码或授权码"):
        update_email_smtp(EmailSmtpPrefsIn(
            host="smtp.example.com",
            username="bot@example.com",
            from_address="bot@example.com",
            to_addresses=["alerts@example.com"],
        ))
