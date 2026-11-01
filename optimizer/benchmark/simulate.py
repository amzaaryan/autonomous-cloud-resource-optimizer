"""Reactive vs predictive scaling benchmark on a simulated plant.

What is REAL here: the decision code. Every dynamic controller calls the same
``reactive_decision`` / ``combined_decision`` the live optimizer calls, every ML forecast comes
from ``ForecastEngine`` loading models written by ``train_models`` (the production training
path), and telemetry rows have the production schema and sampling interval.

What is SIMULATED: the service and the load. The plant is a closed-loop queueing model of the
target-service behind a load balancer (see ``closed_loop``), driven by the same user profiles
as ``load_testing/locustfile.py``. Numbers from this benchmark show how the *policies*
compare under stated assumptions; they are not measurements of your Docker stack.

Fairness rules:
* every controller faces the identical plant, profile and seeds;
* the Random-Forest forecaster for profile P is trained ONLY on other profiles (leave-one-
  profile-out, with jittered variants), so it is always evaluated on a traffic shape it has
  never seen;
* a "rate-based" predictive controller (forecast = current request rate, no ML) is included so
  the value added by machine learning is visible rather than assumed.

Run:  python -m optimizer.benchmark.simulate --seeds 5
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import zlib
from typing import Any

import numpy as np
import pandas as pd

from optimizer.analytics import per_replica_cpu
from optimizer.benchmark.profiles import DURATION_SECONDS, PROFILES, Profile, jittered
from optimizer.forecasting.engine import ForecastEngine
from optimizer.forecasting.train import train_models
from optimizer.policy.combined import combined_decision
from optimizer.policy.policies import PolicyConfig, PolicyInput, reactive_decision


HORIZONS = (1, 3, 5)


@dataclass(frozen=True)
class PlantConfig:
    """The simulated service. Defaults describe one 0.5-core replica at LOCUST_WORK_INTENSITY=low."""

    capacity_rps: float = 115.0  # TRUE max /work rps one replica sustains (~3 ms CPU/request on 0.5 core, less HTTP overhead). ASSUMPTION: calibrate on your machine
    cpu_limit_cores: float = 0.5
    think_time_s: float = 0.275  # mean of Locust between(0.05, 0.5)
    work_fraction: float = 10 / 11  # Locust task weights: 10 /work : 1 /health
    startup_delay_s: float = 12.0  # decision -> replica serving: create+start+readiness poll+DNS refresh
    startup_jitter_s: float = 2.0
    interval_s: int = 15  # optimizer loop / telemetry interval
    rate_window_s: int = 60  # rate(...[1m]) used for request rate and CPU
    latency_window_s: int = 300  # rate(...[5m]) used inside histogram_quantile
    duration_s: int = DURATION_SECONDS
    demand_noise: float = 0.03  # per-second multiplicative noise on active users
    memory_ratio: float = 0.25


@dataclass(frozen=True)
class ControlConfig:
    """Optimizer settings; defaults equal the shipped defaults in ``optimizer.config.Settings``."""

    min_replicas: int = 1
    max_replicas: int = 5
    scale_step: int = 1
    cooldown_seconds: int = 60
    cpu_scale_up_threshold: float = 0.70
    cpu_scale_down_threshold: float = 0.35
    memory_scale_up_threshold: float = 0.75
    latency_sla_ms: float = 500.0
    error_rate_sla: float = 0.05
    capacity_rps_per_replica: float = 90.0  # PLANNING capacity (conservative vs the plant's true 115)
    target_utilization: float = 0.70
    scale_down_utilization: float = 0.50
    cost_per_replica_minute: float = 0.0035

    def policy_config(self, predictive_max_step: int = 0) -> PolicyConfig:
        return PolicyConfig(
            min_replicas=self.min_replicas,
            max_replicas=self.max_replicas,
            scale_step=self.scale_step,
            cooldown_seconds=self.cooldown_seconds,
            cpu_scale_up_threshold=self.cpu_scale_up_threshold,
            cpu_scale_down_threshold=self.cpu_scale_down_threshold,
            memory_scale_up_threshold=self.memory_scale_up_threshold,
            latency_sla_ms=self.latency_sla_ms,
            error_rate_sla=self.error_rate_sla,
            predictive_max_step=predictive_max_step,
        )


class PersistenceForecaster:
    """Rate-based 'prediction': the future equals the present. No ML; a deliberately strong baseline."""

    horizons = HORIZONS

    def forecast(self, history: list[dict[str, Any]]) -> tuple[dict[int, float], float | None]:
        rate = history[-1].get("request_rate") or 0.0
        return {h: float(rate) for h in self.horizons}, 1.0


@dataclass
class Controller:
    name: str
    kind: str  # "static" | "reactive" | "predictive"
    replicas: int = 1  # fixed size for "static"
    forecaster: Any = None
    predictive_max_step: int = 0


@dataclass
class RunResult:
    metrics: dict[str, float]
    telemetry: pd.DataFrame
    timeline: pd.DataFrame


def closed_loop(users: float, ready: int, plant: PlantConfig) -> tuple[float, float, float]:
    """Closed-loop load on ``ready`` replicas -> (/work rps, per-replica utilisation, mean /work latency in s).

    Each Locust user loops: think (mean ``think_time_s``), then issue a request. With balanced
    load every replica is an M/M/1-like queue with service time s0 = 1/capacity, so mean latency
    is R = s0 / (1 - rho). Because users wait for replies, throughput is self-limiting:
    X = w * N / (Z + w * R), and rho = X / (ready * capacity). That is one equation in rho; it is
    solved by bisection (left side decreasing in rho, so the root is unique).
    """
    s0 = 1.0 / plant.capacity_rps
    if users <= 0:
        return 0.0, 0.0, s0
    total_capacity = max(1, ready) * plant.capacity_rps
    w, z = plant.work_fraction, plant.think_time_s
    lo, hi = 0.0, 0.9999
    for _ in range(40):
        mid = (lo + hi) / 2
        throughput = w * users / (z + w * (s0 / (1 - mid)))
        if throughput / total_capacity > mid:
            lo = mid
        else:
            hi = mid
    rho = (lo + hi) / 2
    latency = s0 / (1 - rho)
    return w * users / (z + w * latency), rho, latency


def simulate(
    profile: Profile,
    controller: Controller,
    *,
    plant: PlantConfig = PlantConfig(),
    control: ControlConfig = ControlConfig(),
    seed: int = 0,
) -> RunResult:
    rng = np.random.default_rng(seed)
    horizon = plant.duration_s
    policy_cfg = control.policy_config(controller.predictive_max_step)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)

    static = controller.kind == "static"
    ready = controller.replicas if static else control.min_replicas
    pending: list[float] = []  # absolute times at which replicas start serving
    users = 0.0
    last_scaled_at: datetime | None = None
    actions = 0

    rps = np.zeros(horizon)
    rho_series = np.zeros(horizon)
    p95_series = np.zeros(horizon)
    ready_series = np.zeros(horizon)
    paid_series = np.zeros(horizon)
    user_series = np.zeros(horizon)
    history: list[dict[str, Any]] = []

    for t in range(horizon):
        while pending and min(pending) <= t:
            pending.remove(min(pending))
            ready += 1

        target, spawn = profile(t)
        users = min(target, users + spawn) if users < target else max(target, users - spawn)
        effective = max(0.0, users * (1.0 + rng.normal(0.0, plant.demand_noise)))
        x, rho, latency = closed_loop(effective, ready, plant)
        rps[t], rho_series[t], ready_series[t] = x, rho, ready
        p95_series[t] = 3.0 * latency * 1000.0 if x > 0 else 0.0  # exponential tail: p95 = 3 x mean
        paid_series[t] = ready + len(pending)  # you pay for a replica while it boots, too
        user_series[t] = users

        if static or (t + 1) % plant.interval_s != 0:
            continue

        # ---- optimizer tick: observe exactly what Prometheus would expose -------------------
        a = max(0, t + 1 - plant.rate_window_s)
        b = max(0, t + 1 - plant.latency_window_s)
        cores = float((rho_series[a : t + 1] * ready_series[a : t + 1]).mean() * plant.cpu_limit_cores)
        weight = float(rps[b : t + 1].sum())
        p95_observed = float((rps[b : t + 1] * p95_series[b : t + 1]).sum() / weight) if weight > 1e-9 else None
        data: dict[str, Any] = {
            "timestamp": (start + timedelta(seconds=t + 1)).isoformat(),
            "cpu_usage": per_replica_cpu(cores, ready, plant.cpu_limit_cores),
            "memory_usage": plant.memory_ratio,
            "request_rate": float(rps[a : t + 1].mean()),
            "p95_latency_ms": p95_observed,
            "error_rate": 0.0,
            "replica_count": ready,
        }
        history.append(data)

        forecasts: dict[int, float] = {}
        confidence: float | None = None
        if controller.forecaster is not None:
            forecasts, confidence = controller.forecaster.forecast(history)

        total = ready + len(pending)  # the optimizer counts replicas it already ordered
        policy_input = PolicyInput(
            cpu=data["cpu_usage"],
            memory=data["memory_usage"],
            p95_latency_ms=p95_observed,
            error_rate=0.0,
            current_replicas=total,
            predicted_request_rate=forecasts,
        )
        now = start + timedelta(seconds=t + 1)
        if controller.kind == "reactive":
            decision = reactive_decision(policy_input, policy_cfg, now=now, last_scaled_at=last_scaled_at)
        else:
            decision = combined_decision(
                policy_input,
                policy_cfg,
                now=now,
                last_scaled_at=last_scaled_at,
                forecast_confidence=confidence,
                capacity_rps_per_replica=control.capacity_rps_per_replica,
                target_utilization=control.target_utilization,
                scale_down_utilization=control.scale_down_utilization,
            )

        if decision.action == "scale_up" and decision.desired_replicas > total:
            for _ in range(decision.desired_replicas - total):
                delay = plant.startup_delay_s + rng.uniform(-plant.startup_jitter_s, plant.startup_jitter_s)
                pending.append(t + 1 + delay)
        elif decision.action == "scale_down" and decision.desired_replicas < total:
            for _ in range(total - decision.desired_replicas):
                if pending:
                    pending.remove(max(pending))
                elif ready > control.min_replicas:
                    ready -= 1
        else:
            continue
        last_scaled_at = now
        actions += 1

    violating = p95_series > control.latency_sla_ms
    total_requests = float(rps.sum())
    metrics = {
        "sla_violation_seconds": float(violating.sum()),
        "sla_violation_pct": float(100.0 * violating.mean()),
        "requests_over_sla_pct": float(100.0 * rps[violating].sum() / total_requests) if total_requests > 0 else 0.0,
        "peak_p95_ms": float(p95_series.max()),
        "avg_replicas": float(paid_series.mean()),
        "replica_minutes": float(paid_series.sum() / 60.0),
        "est_cost": float(paid_series.sum() / 60.0 * control.cost_per_replica_minute),
        "scale_actions": float(actions),
    }
    timeline = pd.DataFrame(
        {
            "t": np.arange(horizon),
            "users": user_series,
            "rps": rps,
            "utilisation": rho_series,
            "p95_ms": p95_series,
            "replicas": ready_series,
        }
    )
    return RunResult(metrics=metrics, telemetry=pd.DataFrame(history), timeline=timeline)


# ---------------------------------------------------------------------------------------
# Experiment: leave-one-profile-out forecaster training + policy comparison
# ---------------------------------------------------------------------------------------


def _collect_training_traces(
    profiles: list[str], variants: int, seed_base: int, plant: PlantConfig, control: ControlConfig
) -> list[pd.DataFrame]:
    """Telemetry from reactive runs over jittered copies of the given profiles."""
    frames = []
    for name in profiles:
        for variant in range(variants):
            seed = seed_base + 1000 * variant + zlib.crc32(name.encode()) % 997  # stable across processes
            rng = np.random.default_rng(seed)
            run = simulate(jittered(name, rng), Controller("collect", "reactive"), plant=plant, control=control, seed=seed)
            frames.append(run.telemetry)
    return frames


def _policies(engine: ForecastEngine | None, max_replicas: int) -> list[Controller]:
    controllers = [
        Controller("static-3 (over-provisioned)", "static", replicas=3),
        Controller("static-5 (peak)", "static", replicas=max_replicas),
        Controller("reactive", "reactive"),
        Controller("predictive: rate-based, 1-step", "predictive", forecaster=PersistenceForecaster()),
        Controller(
            "predictive: rate-based, jump",
            "predictive",
            forecaster=PersistenceForecaster(),
            predictive_max_step=max_replicas,
        ),
    ]
    if engine is not None:
        controllers += [
            Controller("predictive: random-forest, 1-step", "predictive", forecaster=engine),
            Controller("predictive: random-forest, jump", "predictive", forecaster=engine, predictive_max_step=max_replicas),
        ]
    return controllers


def run_benchmark(
    *,
    profiles: list[str] | None = None,
    seeds: int = 5,
    train_variants: int = 6,
    plant: PlantConfig = PlantConfig(),
    control: ControlConfig = ControlConfig(),
    with_ml: bool = True,
    target_mode: str = "delta",
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, pd.DataFrame]]:
    """Returns (per-run results, forecast accuracy by held-out profile, timelines of seed 0)."""
    profiles = profiles or list(PROFILES)
    runs: list[dict[str, Any]] = []
    accuracy: list[dict[str, Any]] = []
    timelines: dict[str, pd.DataFrame] = {}

    for held_out in profiles:
        engine: ForecastEngine | None = None
        workdir = tempfile.TemporaryDirectory()
        if with_ml:
            others = [name for name in PROFILES if name != held_out]
            train_frames = _collect_training_traces(others, train_variants, seed_base=10_000, plant=plant, control=control)
            test_run = simulate(PROFILES[held_out], Controller("holdout", "reactive"), plant=plant, control=control, seed=777)
            rows, _ = train_models(
                train_frames,
                horizons=HORIZONS,
                interval_seconds=float(plant.interval_s),
                test_frames=[test_run.telemetry],
                model_dir=workdir.name,
                report_path=None,
                data_source=f"simulated; held-out profile = {held_out}",
                target_mode=target_mode,
            )
            accuracy += [{"held_out_profile": held_out, **row} for row in rows]
            engine = ForecastEngine(workdir.name, HORIZONS, float(plant.interval_s))
            for model in engine.models.values():
                model.n_jobs = 1  # single-row inference is faster without a thread pool

        for controller in _policies(engine, control.max_replicas):
            for seed in range(seeds):
                result = simulate(PROFILES[held_out], controller, plant=plant, control=control, seed=seed)
                runs.append({"profile": held_out, "policy": controller.name, "seed": seed, **result.metrics})
                if seed == 0:
                    timelines[f"{held_out}|{controller.name}"] = result.timeline
        workdir.cleanup()

    return pd.DataFrame(runs), pd.DataFrame(accuracy), timelines


def summarise(runs: pd.DataFrame) -> pd.DataFrame:
    metrics = [c for c in runs.columns if c not in {"profile", "policy", "seed"}]
    grouped = runs.groupby(["profile", "policy"], sort=False)[metrics]
    mean = grouped.mean().add_suffix("_mean")
    std = grouped.std(ddof=0).add_suffix("_std")
    return pd.concat([mean, std], axis=1).reset_index()


def plot_timeline(timelines: dict[str, pd.DataFrame], profile: str, policies: list[str], path: Path) -> bool:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return False

    fig, axes = plt.subplots(3, 1, figsize=(10, 8), sharex=True)
    first = timelines[f"{profile}|{policies[0]}"]
    axes[0].plot(first["t"] / 60, first["users"], color="#555", lw=1.2)
    axes[0].set_ylabel("active users\n(Locust profile)")
    for name in policies:
        frame = timelines[f"{profile}|{name}"]
        axes[1].step(frame["t"] / 60, frame["replicas"], where="post", label=name, lw=1.6)
        axes[2].plot(frame["t"] / 60, frame["p95_ms"].clip(upper=5000), label=name, lw=1.2)
    axes[1].set_ylabel("serving replicas")
    axes[2].axhline(500, color="red", ls="--", lw=1, label="SLA (500 ms)")
    axes[2].set_ylabel("p95 latency (ms)\n(clipped at 5 s)")
    axes[2].set_xlabel("minutes")
    axes[1].legend(fontsize=8, loc="upper right")
    fig.suptitle(f"Simulated '{profile}' profile (seed 0) — simulation, not a Docker measurement")
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=130)
    plt.close(fig)
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Reactive vs predictive scaling benchmark (simulation)")
    parser.add_argument("--seeds", type=int, default=5)
    parser.add_argument("--profiles", nargs="*", default=None, choices=list(PROFILES))
    parser.add_argument("--train-variants", type=int, default=6, help="jittered training traces per other profile")
    parser.add_argument("--no-ml", action="store_true", help="skip Random-Forest controllers")
    parser.add_argument("--target-mode", choices=["delta", "absolute"], default="delta")
    parser.add_argument("--out", default="data/processed")
    parser.add_argument("--plot-dir", default="docs/images")
    args = parser.parse_args()

    runs, accuracy, timelines = run_benchmark(
        profiles=args.profiles, seeds=args.seeds, train_variants=args.train_variants, with_ml=not args.no_ml,
        target_mode=args.target_mode,
    )
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    summary = summarise(runs)
    runs.to_csv(out / "benchmark_runs.csv", index=False)
    summary.to_csv(out / "benchmark_summary.csv", index=False)
    if not accuracy.empty:
        accuracy.to_csv(out / "forecast_accuracy_heldout.csv", index=False)

    show = ["profile", "policy", "sla_violation_seconds_mean", "requests_over_sla_pct_mean", "peak_p95_ms_mean", "avg_replicas_mean", "est_cost_mean", "scale_actions_mean"]
    with pd.option_context("display.width", 220, "display.max_columns", 20, "display.float_format", "{:.1f}".format):
        print(summary[show].to_string(index=False))
        if not accuracy.empty:
            print()
            print(accuracy[["held_out_profile", "horizon_min", "mae", "baseline_mae", "r2", "baseline_r2", "mae_improvement_vs_baseline_pct"]].to_string(index=False))

    if timelines:
        shown = [p for p in ("spike", "burst") if any(k.startswith(f"{p}|") for k in timelines)]
        for profile in shown:
            wanted = [n for n in ("static-3 (over-provisioned)", "reactive", "predictive: random-forest, jump") if f"{profile}|{n}" in timelines]
            if wanted and plot_timeline(timelines, profile, wanted, Path(args.plot_dir) / f"benchmark_{profile}.png"):
                print(f"wrote {args.plot_dir}/benchmark_{profile}.png")
    print(f"\nwrote {out}/benchmark_runs.csv, benchmark_summary.csv" + ("" if accuracy.empty else ", forecast_accuracy_heldout.csv"))


if __name__ == "__main__":
    main()
