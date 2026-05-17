from __future__ import annotations

import argparse

from early_trade_label.build_dataset import build_dataset
from early_trade_label.config import load_config
from early_trade_label.train_model import train
from early_trade_label.tune import run_tuning_audit
from execution_engine.artifact_export import export_artifact
from execution_engine.maker_fill_table import generate_maker_fill_table


def run_all(config_path: str) -> None:
    cfg = load_config(config_path)
    cfg.processed_dir.mkdir(parents=True, exist_ok=True)
    cfg.outdir.mkdir(parents=True, exist_ok=True)
    build_dataset(cfg.trades_csv, cfg.refs_csv, cfg.processed_dir, cfg.feature_window_seconds)
    train(
        dataset_path=cfg.processed_dir / "market_dataset.parquet",
        outdir=cfg.outdir,
        project=cfg.project,
        train_fraction=cfg.train_fraction,
        purge_minutes=cfg.purge_minutes,
        min_coverage=cfg.min_coverage,
        threshold_step=cfg.threshold_step,
        random_state=cfg.random_state,
        feature_window_seconds=cfg.feature_window_seconds,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run-all")
    p.add_argument("--config", required=True)
    fill = sub.add_parser("maker-fill-table")
    fill.add_argument("--trades-csv", required=True)
    fill.add_argument("--out", required=True)
    fill.add_argument("--decision-second", type=int, default=120)
    exp = sub.add_parser("export-artifact")
    exp.add_argument("--model-dir", required=True)
    exp.add_argument("--deploy-dir", default="execution_engine/deploy")
    exp.add_argument("--model-version")
    exp.add_argument("--maker-fill-table")
    exp.add_argument("--bundle-out")
    tune = sub.add_parser("tune-audit")
    tune.add_argument("--dataset", required=True)
    tune.add_argument("--outdir", required=True)
    tune.add_argument("--train-fraction", type=float, default=0.70)
    tune.add_argument("--purge-minutes", type=int, default=10)
    tune.add_argument("--min-coverage", type=float, default=0.70)
    tune.add_argument("--threshold-step", type=float, default=0.005)
    tune.add_argument("--random-state", type=int, default=42)
    args = parser.parse_args()
    if args.cmd == "run-all":
        run_all(args.config)
    elif args.cmd == "maker-fill-table":
        from pathlib import Path

        generate_maker_fill_table(Path(args.trades_csv), Path(args.out), args.decision_second)
    elif args.cmd == "export-artifact":
        from pathlib import Path

        export_artifact(
            Path(args.model_dir),
            Path(args.deploy_dir),
            args.model_version,
            Path(args.maker_fill_table) if args.maker_fill_table else None,
            Path(args.bundle_out) if args.bundle_out else None,
        )
    elif args.cmd == "tune-audit":
        from pathlib import Path

        run_tuning_audit(
            Path(args.dataset),
            Path(args.outdir),
            args.train_fraction,
            args.purge_minutes,
            args.min_coverage,
            args.threshold_step,
            args.random_state,
        )


if __name__ == "__main__":
    main()
