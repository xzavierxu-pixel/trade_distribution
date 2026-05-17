from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class PipelineConfig:
    project: str
    trades_csv: Path
    refs_csv: Path
    outdir: Path
    processed_dir: Path
    feature_window_seconds: int = 120
    label_positive: str = "up"
    train_fraction: float = 0.70
    purge_minutes: int = 10
    min_coverage: float = 0.70
    threshold_step: float = 0.005
    baseline_accuracy: str = "majority_class"
    random_state: int = 42


def _load_raw(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        return json.loads(text)
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(text)
        return data or {}
    except Exception:
        return _minimal_yaml(text)


def _minimal_yaml(text: str) -> dict[str, Any]:
    data: dict[str, Any] = {}
    stack: list[tuple[int, dict[str, Any]]] = [(-1, data)]
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        line = raw.strip()
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]
        if value == "":
            child: dict[str, Any] = {}
            parent[key] = child
            stack.append((indent, child))
        else:
            parent[key] = _coerce_scalar(value)
    return data


def _coerce_scalar(value: str) -> Any:
    value = value.strip().strip('"').strip("'")
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


def load_config(path: str | Path) -> PipelineConfig:
    cfg_path = Path(path)
    raw = _load_raw(cfg_path)
    split = raw.get("time_split", {})
    th = raw.get("threshold_search", {})
    metrics = raw.get("metrics", {})
    return PipelineConfig(
        project=str(raw.get("project", "btc-polymarket-early-trade-v1")),
        trades_csv=Path(raw["trades_csv"]),
        refs_csv=Path(raw["refs_csv"]),
        outdir=Path(raw.get("outdir", "models/early_trade_label_v1")),
        processed_dir=Path(raw.get("processed_dir", "data_processed/early_trade_label_v1")),
        feature_window_seconds=int(raw.get("feature_window_seconds", 120)),
        label_positive=str(raw.get("label_positive", "up")),
        train_fraction=float(split.get("train_fraction", 0.70)),
        purge_minutes=int(split.get("purge_minutes", 10)),
        min_coverage=float(th.get("min_coverage", 0.70)),
        threshold_step=float(th.get("step", 0.005)),
        baseline_accuracy=str(metrics.get("baseline_accuracy", "majority_class")),
        random_state=int(raw.get("random_state", 42)),
    )
