from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Magma execution
    magma_timeout: int = 120
    magma_cpu_timeout: int = 120
    magma_memory_mb: int = 400
    magma_input_kb: int = 50
    magma_output_kb: int = 20

    # Service
    max_concurrent: int = 4
    port: int = 8080

    # Rate limiting
    rate_limit_per_minute: int = 30
    rate_limit_per_hour: int = 200

    # CORS
    allowed_origin: str = "*"

    # Usage logging
    usage_log_file: str = "/data/usage.jsonl"

    # Optional Turnstile
    turnstile_enabled: bool = False
    turnstile_secret_key: str = ""

    @property
    def allowed_origins_list(self) -> list[str]:
        return [o.strip() for o in self.allowed_origin.split(",")]

    @property
    def magma_input_bytes(self) -> int:
        return self.magma_input_kb * 1024

    @property
    def magma_output_bytes(self) -> int:
        return self.magma_output_kb * 1024
