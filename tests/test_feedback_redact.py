"""Tests for the feedback redaction engine."""

from __future__ import annotations

from pathlib import Path

from swarm.feedback.redact import redact_config_dict, redact_text


def test_redact_home_path(monkeypatch):
    monkeypatch.setenv("HOME", "/home/alice")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/home/alice")))
    text = "Error at /home/alice/projects/swarm/foo.py line 42"
    out, count = redact_text(text)
    assert "/home/alice" not in out
    assert "~/projects/swarm/foo.py" in out
    assert count >= 1


def test_redact_github_token():
    text = "Using token ghp_" + "a" * 36 + " for API calls"
    out, count = redact_text(text)
    assert "<github-token>" in out
    assert "ghp_" not in out
    assert count >= 1


def test_redact_anthropic_key():
    text = "API key: sk-ant-abcdefghijklmnopqrstuvwxyz"
    out, count = redact_text(text)
    assert "<api-key>" in out
    assert "sk-ant" not in out
    assert count >= 1


def test_redact_openai_key():
    text = "sk-proj-abcdefghijklmnopqrstuvwxyz1234567890"
    out, count = redact_text(text)
    assert "<api-key>" in out
    assert count >= 1


def test_redact_aws_key():
    text = "AWS_ACCESS_KEY=AKIAIOSFODNN7EXAMPLE and more"
    out, count = redact_text(text)
    assert "AKIA" not in out
    assert "<aws-key>" in out
    assert count >= 1


def test_redact_jwt():
    # Fake but shape-valid JWT
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NSJ9.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    text = f"Authorization: Bearer {jwt}"
    out, count = redact_text(text)
    assert "eyJhbGciOiJIUzI1NiJ9" not in out
    assert count >= 1


def test_redact_bearer_token():
    text = "Authorization: Bearer abcdefghijklmnopqrstuvwxyz12345"
    out, count = redact_text(text)
    assert "Bearer <redacted>" in out
    assert count >= 1


def test_redact_generic_hex():
    text = "Session id: " + "a" * 40
    out, count = redact_text(text)
    assert "<hex-secret>" in out
    assert count >= 1


def test_redact_email():
    text = "Contact user@example.com for details"
    out, count = redact_text(text)
    assert "user@example.com" not in out
    assert "<email>" in out
    assert count >= 1


def test_redact_auth_url():
    text = "Cloning from https://alice:supersecret@github.com/repo.git"
    out, count = redact_text(text)
    assert "supersecret" not in out
    assert "<redacted>@github.com" in out
    assert count >= 1


def test_redact_env_var_value(monkeypatch):
    monkeypatch.setenv("MY_SECRET", "hunter2-mega-password")
    text = "Connecting with password hunter2-mega-password to db"
    out, count = redact_text(text, env_refs=["MY_SECRET"])
    assert "hunter2-mega-password" not in out
    assert "<env-secret>" in out
    assert count >= 1


def test_redact_env_var_ignores_short_values(monkeypatch):
    """Short env values should not be scrubbed (too many false positives)."""
    monkeypatch.setenv("DEBUG", "1")
    text = "DEBUG=1 verbose mode"
    out, _ = redact_text(text, env_refs=["DEBUG"])
    # "1" should still be present — we don't scrub trivially short values
    assert "1" in out


def test_redact_preserves_non_sensitive_text():
    text = "The swarm daemon started successfully on port 9090."
    out, count = redact_text(text)
    assert out == text
    assert count == 0


def test_redact_empty_string():
    out, count = redact_text("")
    assert out == ""
    assert count == 0


def test_redact_config_dict_blanks_sensitive_keys():
    data = {
        "jira": {
            "url": "https://example.atlassian.net",
            "client_id": "pub-abc",
            "client_secret": "super-secret-value",
            "api_token": "token-abc",
        },
        "workers": [{"name": "worker1", "password": "pw"}],
        "log_level": "INFO",
    }
    out, count = redact_config_dict(data)
    assert isinstance(out, dict)
    assert out["jira"]["url"] == "https://example.atlassian.net"
    assert out["jira"]["client_secret"] == "<redacted>"
    assert out["jira"]["api_token"] == "<redacted>"
    assert out["workers"][0]["password"] == "<redacted>"
    assert out["workers"][0]["name"] == "worker1"
    assert out["log_level"] == "INFO"
    assert count >= 3


def test_redact_config_dict_skips_empty_values():
    """Empty/None values under sensitive keys should not be counted."""
    data = {"token": "", "password": None, "api_key": "real-value-here"}
    out, count = redact_config_dict(data)
    assert isinstance(out, dict)
    assert out["token"] == ""
    assert out["password"] is None
    assert out["api_key"] == "<redacted>"
    assert count == 1


# --- D: coverage for the GitHub/AWS patterns that existed but were untested ---


def test_redact_github_fine_grained_pat():
    text = "tok github_pat_" + "A" * 82 + " end"
    out, count = redact_text(text)
    assert "<github-token>" in out and "github_pat_" not in out and count >= 1


def test_redact_github_oauth_and_server_tokens():
    for prefix in ("gho_", "ghs_"):
        out, count = redact_text(f"token {prefix}{'b' * 36}")
        assert "<github-token>" in out and prefix not in out and count >= 1


def test_redact_aws_secret_access_key():
    text = "aws_secret_access_key=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
    out, count = redact_text(text)
    assert "wJalrXUtnFEMI" not in out
    assert "aws_secret_access_key=<redacted>" in out and count >= 1


# --- B: webhook URLs (tokens in path or query) ---


def test_redact_slack_webhook_url():
    text = "POST https://hooks.slack.com/services/T00000/B00000/XXXXSECRETxxxx failed"
    out, count = redact_text(text)
    assert "XXXXSECRETxxxx" not in out
    assert "<slack-webhook>" in out and count >= 1


def test_redact_discord_webhook_url():
    text = "url=https://discord.com/api/webhooks/123456789012/AbCdEfSECRET_token-xyz"
    out, count = redact_text(text)
    assert "AbCdEfSECRET_token-xyz" not in out
    assert "<discord-webhook>" in out and count >= 1


def test_redact_token_in_query_param():
    text = "ntfy https://ntfy.sh/mytopic?auth=tk_SECRETvalue123 and ?token=ABCSECRET999"
    out, count = redact_text(text)
    assert "tk_SECRETvalue123" not in out
    assert "ABCSECRET999" not in out
    assert count >= 2
