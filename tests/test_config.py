from app.config import Settings


def test_default_settings():
    settings = Settings()
    assert settings.magma_timeout == 120
    assert settings.magma_cpu_timeout == 120
    assert settings.magma_memory_mb == 400
    assert settings.magma_input_kb == 50
    assert settings.magma_output_kb == 20
    assert settings.max_concurrent == 4
    assert settings.port == 8080
    assert settings.rate_limit_per_minute == 30
    assert settings.rate_limit_per_hour == 200
    assert settings.allowed_origin == "https://magma-maths.org,http://localhost"
    assert settings.turnstile_enabled is False
    assert settings.turnstile_secret_key == ""
    assert settings.tls_cert_file == "/certs/live/calc.magma-maths.org/fullchain.pem"
    assert settings.tls_key_file == "/certs/live/calc.magma-maths.org/privkey.pem"


def test_settings_from_env(monkeypatch):
    monkeypatch.setenv("MAGMA_TIMEOUT", "300")
    monkeypatch.setenv("MAGMA_MEMORY_MB", "800")
    monkeypatch.setenv("ALLOWED_ORIGIN", "https://example.com")
    settings = Settings()
    assert settings.magma_timeout == 300
    assert settings.magma_memory_mb == 800
    assert settings.allowed_origin == "https://example.com"


def test_allowed_origins_list():
    settings = Settings()
    origins = settings.allowed_origins_list
    assert "https://magma-maths.org" in origins
    assert "http://localhost" in origins
