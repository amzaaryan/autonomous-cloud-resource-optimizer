"""Runtime configuration for the optimizer."""

from dataclasses import dataclass
import os


@dataclass(frozen=True)
class Settings:
    prometheus_url: str = os.getenv("PROMETHEUS_URL", "http://localhost:9090")
    target_service_name: str = os.getenv("TARGET_SERVICE_NAME", "target-service")
    min_replicas: int = int(os.getenv("MIN_REPLICAS", "1"))
    max_replicas: int = int(os.getenv("MAX_REPLICAS", "5"))
    scale_step: int = int(os.getenv("SCALE_STEP", "1"))
    cooldown_seconds: int = int(os.getenv("COOLDOWN_SECONDS", "60"))
    cpu_scale_up_threshold: float = float(os.getenv("CPU_SCALE_UP_THRESHOLD", "0.70"))
    cpu_scale_down_threshold: float = float(os.getenv("CPU_SCALE_DOWN_THRESHOLD", "0.35"))
    latency_sla_ms: float = float(os.getenv("LATENCY_SLA_MS", "500"))
    dry_run: bool = os.getenv("DRY_RUN", "true").lower() == "true"
