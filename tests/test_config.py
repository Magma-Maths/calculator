import re
from pathlib import Path

from app.config import Settings

ROOT = Path(__file__).resolve().parent.parent


def test_default_settings():
    settings = Settings()
    assert settings.magma_timeout == 120
    assert settings.magma_cpu_timeout == 120
    assert settings.magma_input_kb == 50
    assert settings.magma_output_kb == 20
    assert settings.max_concurrent == 4
    assert settings.port == 8080
    assert settings.rate_limit_per_minute == 30
    assert settings.rate_limit_per_hour == 200
    assert settings.allowed_origin == "*"
    assert settings.submission_log_file == ""


def test_settings_from_env(monkeypatch):
    monkeypatch.setenv("MAGMA_TIMEOUT", "300")
    monkeypatch.setenv("MAGMA_CPU_TIMEOUT", "60")
    monkeypatch.setenv("ALLOWED_ORIGIN", "https://example.com")
    settings = Settings()
    assert settings.magma_timeout == 300
    assert settings.magma_cpu_timeout == 60
    assert settings.allowed_origin == "https://example.com"


def test_allowed_origins_list():
    settings = Settings()
    origins = settings.allowed_origins_list
    assert "*" in origins


def _parse_readme_defaults():
    """Parse the defaults table from README.md into {ENV_VAR: default_str}."""
    text = (ROOT / "README.md").read_text()
    rows = re.findall(
        r"^\| `(\w+)` \| (.+?) \| .+ \|$", text, re.MULTILINE
    )
    defaults = {}
    for var, val in rows:
        # Strip backticks and surrounding whitespace
        value = val.strip().strip("`")
        defaults[var] = {"empty": "", "unset": "None"}.get(value, value)
    return defaults


def test_readme_defaults_match_settings():
    """Ensure the README defaults table stays in sync with Settings."""
    settings = Settings()
    readme = _parse_readme_defaults()
    assert readme, "Could not parse any defaults from README.md"
    for env_var, readme_val in readme.items():
        field_name = env_var.lower()
        actual = getattr(settings, field_name)
        assert str(actual) == readme_val, (
            f"{env_var}: README says {readme_val!r}, "
            f"Settings has {actual!r}"
        )


def test_allowed_origins_list_custom(monkeypatch):
    monkeypatch.setenv("ALLOWED_ORIGIN", "https://magma-maths.org,http://localhost")
    settings = Settings()
    origins = settings.allowed_origins_list
    assert "https://magma-maths.org" in origins
    assert "http://localhost" in origins
