import pytest

TEST_ENVIRONMENT = {
    "APP_ENV": "test",
    "SECRET_KEY": "test-secret-key",
    "SMTP_HOST": "smtp.invalid",
    "EMAIL_FROM": "noreply@example.com",
}


def set_test_environment(monkeypatch: pytest.MonkeyPatch, database_url: str) -> None:
    for name, value in TEST_ENVIRONMENT.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("DATABASE_URL", database_url)
