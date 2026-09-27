from pydantic import model_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Magma execution
    magma_root: str = "/opt/magma/current"
    magma_timeout: int = 120
    magma_cpu_timeout: int = 120
    magma_memory_mb: int = 400
    magma_pids_max: int = 64
    magma_cpu_ms_per_sec: int = 1000
    magma_input_kb: int = 50
    magma_output_kb: int = 20
    magma_capture_kb: int = 256

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

    @model_validator(mode="after")
    def validate_executor_limits(self):
        if any(value <= 0 for value in (
            self.magma_pids_max, self.magma_cpu_ms_per_sec,
            self.magma_output_kb, self.magma_capture_kb,
        )):
            raise ValueError("Executor limits must be positive")
        if self.magma_capture_kb < self.magma_output_kb:
            raise ValueError("MAGMA_CAPTURE_KB must be at least MAGMA_OUTPUT_KB")
        return self

    @property
    def allowed_origins_list(self) -> list[str]:
        return [o.strip() for o in self.allowed_origin.split(",")]

    @property
    def magma_input_bytes(self) -> int:
        return self.magma_input_kb * 1024

    @property
    def magma_output_bytes(self) -> int:
        return self.magma_output_kb * 1024

    @property
    def magma_capture_bytes(self) -> int:
        return self.magma_capture_kb * 1024
