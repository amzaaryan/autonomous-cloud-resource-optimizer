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
    target_health_path: str = os.getenv("TARGET_HEALTH_PATH", "/health")

    min_replicas: int = int(os.getenv("MIN_REPLICAS", "1"))
    max_replicas: int = int(os.getenv("MAX_REPLICAS", "5"))
    scale_step: int = max(1, int(os.getenv("SCALE_STEP", "1")))
    cooldown_seconds: int = int(os.getenv("COOLDOWN_SECONDS", "60"))
    # 0 = same as SCALE_STEP. Max replicas the predictive policy may add per decision.
    predictive_max_step: int = int(os.getenv("PREDICTIVE_MAX_STEP", "0"))

    # CPU is evaluated as utilisation of ONE replica's CPU limit (0.0 - 1.0),
    # i.e. aggregate cores used / (replicas * REPLICA_CPU_LIMIT_CORES).
    cpu_scale_up_threshold: float = float(os.getenv("CPU_SCALE_UP_THRESHOLD", "0.70"))
    cpu_scale_down_threshold: float = float(os.getenv("CPU_SCALE_DOWN_THRESHOLD", "0.35"))
    memory_scale_up_threshold: float = float(os.getenv("MEMORY_SCALE_UP_THRESHOLD", "0.75"))
    latency_sla_ms: float = float(os.getenv("LATENCY_SLA_MS", "500"))
    error_rate_sla: float = float(os.getenv("ERROR_RATE_SLA", "0.05"))
    # Must match the CPU limit given to each target-service replica in docker-compose.yml.
    replica_cpu_limit_cores: float = float(os.getenv("REPLICA_CPU_LIMIT_CORES", "0.5"))

    # Capacity model used by the predictive policy: requests/s ONE replica sustains while still
    # meeting the latency SLA. ASSUMPTION - calibrate on your machine with
    # `python load_testing/calibrate.py` (it measures one replica and prints a value).
    # Rough guide for a 0.5-core replica: LOCUST_WORK_INTENSITY=low ~90-130 rps,
    # medium ~20-28 rps, high ~5-7 rps. The Locust profiles need hundreds of rps at their peaks,
    # so only `low` lets 1-5 replicas span their range.
    rps_per_replica_capacity: float = float(os.getenv("RPS_PER_REPLICA_CAPACITY", "90"))
    target_utilization: float = float(os.getenv("TARGET_UTILIZATION", "0.70"))
    scale_down_utilization: float = float(os.getenv("SCALE_DOWN_UTILIZATION", "0.50"))

    telemetry_interval_seconds: int = int(os.getenv("TELEMETRY_INTERVAL_SECONDS", "15"))
    forecast_horizons_minutes: tuple[int, ...] = tuple(
        int(h.strip())
        for h in os.getenv("FORECAST_HORIZONS_MINUTES", "1,3,5").split(",")
        if h.strip()
    )

    dry_run: bool = _get_bool("DRY_RUN", True)
    scaling_mode: str = os.getenv("SCALING_MODE", "compose").strip().lower()
    replica_ready_timeout_seconds: int = int(os.getenv("REPLICA_READY_TIMEOUT_SECONDS", "45"))
    # On scale-down a replica is detached from the network, then given this long for the load
    # balancer's DNS cache to forget it and for in-flight requests to finish, before it is stopped.
    scale_down_drain_seconds: float = float(os.getenv("SCALE_DOWN_DRAIN_SECONDS", "6"))

    model_dir: str = os.getenv("MODEL_DIR", "data/models")
    telemetry_path: str = os.getenv("TELEMETRY_PATH", "data/runtime/telemetry_snapshots.jsonl")
    scaling_events_path: str = os.getenv("SCALING_EVENTS_PATH", "data/runtime/scaling_events.jsonl")

    # Demo control file (traffic preset, policy mode) shared with the dashboard and Locust.
    control_path: str = os.getenv("CONTROL_PATH", "data/runtime/control.json")

    # Background retraining on the telemetry this optimizer collects.
    auto_train: bool = _get_bool("AUTO_TRAIN", True)
    auto_train_min_rows: int = int(os.getenv("AUTO_TRAIN_MIN_ROWS", "80"))  # ~20 min at 15 s sampling
    auto_train_interval_minutes: float = float(os.getenv("AUTO_TRAIN_INTERVAL_MINUTES", "10"))
    # Deploy a model only if it beats the "rate stays the same" baseline on held-out rows.
    auto_train_require_improvement: bool = _get_bool("AUTO_TRAIN_REQUIRE_IMPROVEMENT", True)
    model_status_path: str = os.getenv("MODEL_STATUS_PATH", "data/runtime/model_status.json")
    model_report_path: str = os.getenv("MODEL_REPORT_PATH", "data/processed/model_evaluation.csv")

    cost_per_replica_minute: float = float(os.getenv("COST_PER_REPLICA_MINUTE", "0.0035"))
    savings_baseline_replicas: int = int(os.getenv("SAVINGS_BASELINE_REPLICAS", "3"))
