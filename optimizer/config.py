"""Runtime configuration for the optimizer."""

from dataclasses import dataclass
import os


def _get_bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    prometheus_url: str = os.getenv("PROMETHEUS_URL", "http://localhost:9090")
    optimizer_host: str = os.getenv("OPTIMIZER_HOST", "0.0.0.0")
    optimizer_port: int = int(os.getenv("OPTIMIZER_PORT", "8001"))

    target_service_name: str = os.getenv("TARGET_SERVICE_NAME", "target-service")
    target_service_port: int = int(os.getenv("TARGET_SERVICE_PORT", "8000"))

    min_replicas: int = int(os.getenv("MIN_REPLICAS", "1"))
    max_replicas: int = int(os.getenv("MAX_REPLICAS", "5"))
    scale_step: int = max(1, int(os.getenv("SCALE_STEP", "1")))
    cooldown_seconds: int = int(os.getenv("COOLDOWN_SECONDS", "60"))

    cpu_scale_up_threshold: float = float(os.getenv("CPU_SCALE_UP_THRESHOLD", "0.70"))
    cpu_scale_down_threshold: float = float(os.getenv("CPU_SCALE_DOWN_THRESHOLD", "0.35"))
    memory_scale_up_threshold: float = float(os.getenv("MEMORY_SCALE_UP_THRESHOLD", "0.75"))
    latency_sla_ms: float = float(os.getenv("LATENCY_SLA_MS", "500"))
    error_rate_sla: float = float(os.getenv("ERROR_RATE_SLA", "0.05"))

    telemetry_interval_seconds: int = int(os.getenv("TELEMETRY_INTERVAL_SECONDS", "15"))
    forecast_horizons_minutes: tuple[int, ...] = tuple(
        int(h.strip())
        for h in os.getenv("FORECAST_HORIZONS_MINUTES", "1,3,5").split(",")
        if h.strip()
    )

    dry_run: bool = _get_bool("DRY_RUN", True)
    scaling_mode: str = os.getenv("SCALING_MODE", "compose").strip().lower()

    model_dir: str = os.getenv("MODEL_DIR", "data/models")
    telemetry_path: str = os.getenv("TELEMETRY_PATH", "data/runtime/telemetry_snapshots.jsonl")
    scaling_events_path: str = os.getenv("SCALING_EVENTS_PATH", "data/runtime/scaling_events.jsonl")

    cost_per_replica_minute: float = float(os.getenv("COST_PER_REPLICA_MINUTE", "0.0035"))
    savings_baseline_replicas: int = int(os.getenv("SAVINGS_BASELINE_REPLICAS", "3"))
