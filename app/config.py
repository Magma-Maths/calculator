from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Magma execution
    magma_timeout: int = 120
    magma_cpu_timeout: int = 120
    magma_input_kb: int = 50
    magma_output_kb: int = 20
    supervisor_socket: str = "/run/magma-fc/supervisor.sock"
    # JSON written by the worker's host checks; /health/deep fails while it
    # lists problems. Empty skips it, as on hosts without those checks.
    health_status_file: str = ""
    health_reject_pattern: str = ""

    # Service
    calculator_version: str = "unknown"
    max_concurrent: int = 4
    port: int = 8080
    # Peers whose X-Forwarded-For names the client: the worker's reverse
    # proxy, pinned at this address by the deployment.
    forwarded_allow_ips: str = "172.30.0.2"

    # Rate limiting
    rate_limit_per_minute: int = 30
    rate_limit_per_hour: int = 200

    # CORS
    allowed_origin: str = "*"

    # Usage logging
    usage_log_file: str = "/data/usage.jsonl"

    # Submission logging (full code, for abuse investigation). Off unless set.
    submission_log_file: str = ""

    @property
    def allowed_origins_list(self) -> list[str]:
        return [o.strip() for o in self.allowed_origin.split(",")]

    @property
    def magma_input_bytes(self) -> int:
        return self.magma_input_kb * 1024

    @property
    def magma_output_bytes(self) -> int:
        return self.magma_output_kb * 1024
