"""Regenerate evaluation addenda without rerunning the frozen system report."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
from xgboost import XGBClassifier

from aml.baseline.threshold import BaselineConfig, baseline
from aml.cluster.merge import MergeConfig, merge_hits
from aml.config import Settings
from aml.data.fx import fit_fx
from aml.data.patterns_parser import Attempt
from aml.data.split import TemporalSplit
from aml.detect.common import Hit
from aml.eval.evasion import run as run_evasion
from aml.eval.frozen import frozen_hash
from aml.eval.metrics import binary_metrics, choose_cutoff
from aml.eval.report_metrics import attempt_coverage_report, cluster_precision_summary
from aml.graph.build import TemporalGraph, build_graph
from aml.score.features import FeatureConfig, features_for_groups
from aml.score.model import probabilities
from aml.score.risk import RiskConfig, transparent_score

Z_95 = 1.959963984540054


def wilson_interval(successes: int, trials: int, z: float = Z_95) -> tuple[float, float] | None:
    """Two-sided Wilson score interval for a binomial proportion."""
    if trials <= 0:
        return None
    if not 0 <= successes <= trials:
        raise ValueError("successes must be between zero and trials")
    p = successes / trials
    z2 = z * z
    denominator = 1 + z2 / trials
    center = (p + z2 / (2 * trials)) / denominator
    radius = z * np.sqrt(p * (1 - p) / trials + z2 / (4 * trials * trials)) / denominator
    lower = 0.0 if successes == 0 else max(0.0, float(center - radius))
    upper = 1.0 if successes == trials else min(1.0, float(center + radius))
    return lower, upper


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object in {path}")
    return value


def _write_json_md(directory: Path, stem: str, payload: dict[str, Any], markdown: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{stem}.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    (directory / f"{stem}.md").write_text(markdown.rstrip() + "\n", encoding="utf-8")


def _attempt_intervals(report: dict[str, Any]) -> dict[str, Any]:
    by_typology = report.get("attempt_recall_edge_coverage_tau_0_5", {})
    rows: dict[str, Any] = {}
    for typology, result in by_typology.items():
        thresholds = result.get("thresholds", [])
        metric = next((entry for entry in thresholds if float(entry.get("tau", -1)) == 0.5), None)
        if result.get("status") != "covered" or metric is None:
            rows[typology] = {"status": result.get("status", "not covered"),
                "attempts": result.get("attempts", 0), "detected": None, "recall": None, "ci95": None}
            continue
        detected, attempts = int(metric["edge_detected"]), int(result["attempts"])
        ci = wilson_interval(detected, attempts)
        rows[typology] = {"status": "covered", "attempts": attempts, "detected": detected,
            "recall": detected / attempts if attempts else None,
            "ci95": list(ci) if ci else None}
    return {"source": "outputs/m11/final_report.json", "period": "test",
        "metric": "edge attempt recall at 50% edge coverage", "confidence_level": 0.95,
        "test_evaluated_by_generator": False, "typologies": rows}


def _hits(settings: Settings) -> list[Hit]:
    hits: list[Hit] = []
    for folder in ("fan", "scatter_gather", "cycle", "pass_through"):
        path = settings.output_dir / folder / "hits.json"
        if not path.is_file():
            raise FileNotFoundError(f"detector output required for validation ablation: {path}")
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                stripped = line.strip().rstrip(",")
                if stripped and stripped not in {"[", "]"}:
                    value = json.loads(stripped)
                    if isinstance(value, dict):
                        hits.append(Hit.model_validate(value))
    return hits


def _period_graph(settings: Settings, split: TemporalSplit, period: str) -> TemporalGraph:
    start, end = split.bounds(period)  # type: ignore[arg-type]
    frame = pq.read_table(settings.data.processed_dir / "transactions.parquet",
        filters=[("ts", ">=", start), ("ts", "<", end)]).to_pandas()
    if frame.empty:
        raise ValueError(f"no transactions in {period} period")
    return build_graph(frame)


def _period_clusters(graph: TemporalGraph, hits: list[Hit], start: int, end: int,
                     merge_config: MergeConfig) -> tuple[list[Hit], list[Any]]:
    selected = [hit for hit in hits if start <= hit.t_start <= hit.t_end < end]
    allowed_edges = set(int(tx) for tx in graph.tx_id)
    selected = [hit for hit in selected if set(hit.edge_ids) <= allowed_edges]
    clusters = merge_hits(graph, selected, merge_config)
    return selected, clusters


def _cluster_labels(graph: TemporalGraph, clusters: list[Any], tau: float) -> np.ndarray:
    labels: list[int] = []
    for cluster in clusters:
        positions = graph.positions_for_tx_ids(np.asarray(cluster.edge_ids, dtype=np.int64))
        labels.append(int(bool(len(positions)) and float(graph.label[positions].mean()) >= tau))
    return np.asarray(labels, dtype=np.int8)


def _cluster_features(graph: TemporalGraph, hits: list[Hit], clusters: list[Any],
                      banks: dict[int, str], settings: Settings, fx) -> pd.DataFrame:
    return features_for_groups(graph, [cluster.edge_ids for cluster in clusters],
        [[hits[index] for index in cluster.hit_ids] for cluster in clusters], banks,
        FeatureConfig(**settings.scoring["features"]), fx)


def _ablation(settings: Settings, split: TemporalSplit, all_hits: list[Hit]) -> dict[str, Any]:
    train_start, train_end = split.bounds("train")
    val_start, val_end = split.bounds("validation")
    train_graph, val_graph = _period_graph(settings, split, "train"), _period_graph(settings, split, "validation")
    train_hits, train_clusters = _period_clusters(train_graph, all_hits, train_start, train_end,
        MergeConfig(**settings.detectors["merge"]))
    val_hits, val_clusters = _period_clusters(val_graph, all_hits, val_start, val_end,
        MergeConfig(**settings.detectors["merge"]))
    if not train_clusters or not val_clusters:
        raise ValueError("train and validation detector clusters are both required for the ablation")

    account_table = pq.read_table(settings.data.processed_dir / "accounts.parquet").to_pandas()
    banks = dict(zip(account_table.account_id.astype(int), account_table.bank.astype(str), strict=True))
    fx = fit_fx(train_graph, train_end)
    train_x = _cluster_features(train_graph, train_hits, train_clusters, banks, settings, fx)
    val_x = _cluster_features(val_graph, val_hits, val_clusters, banks, settings, fx)
    train_y = _cluster_labels(train_graph, train_clusters, settings.evaluation["cluster_tau"])
    val_y = _cluster_labels(val_graph, val_clusters, settings.evaluation["cluster_tau"])
    if len(np.unique(train_y)) < 2:
        raise ValueError("train detector clusters need positive and negative labels")

    risk = RiskConfig(**settings.scoring["risk"])
    train_transparent = np.asarray([transparent_score(row, risk) for row in train_x.to_dict("records")])
    val_transparent = np.asarray([transparent_score(row, risk) for row in val_x.to_dict("records")])

    m8_path = settings.output_dir / "m8" / "cluster_model.json"
    frozen_path = settings.output_dir / "m9" / "frozen.yaml"
    frozen = _json(frozen_path)
    model_info = frozen["model"]
    tuned_path = settings.output_dir / "m9" / f"model_{model_info['n_estimators']}_{model_info['max_depth']}.json"
    if not m8_path.is_file() or not tuned_path.is_file():
        raise FileNotFoundError("saved M8/M9 XGBoost model artifacts are required for the ablation")
    m8_model = XGBClassifier()
    m8_model.load_model(m8_path)
    train_m9_x = train_x.copy()
    val_m9_x = val_x.copy()
    train_m9_x["model_probability"] = probabilities(m8_model, train_x)
    val_m9_x["model_probability"] = probabilities(m8_model, val_x)
    m9_model = XGBClassifier()
    m9_model.load_model(tuned_path)
    train_xgb = probabilities(m9_model, train_m9_x)
    val_xgb = probabilities(m9_model, val_m9_x)

    transparent_cutoff, _ = choose_cutoff(train_y, train_transparent)
    xgb_cutoff, _ = choose_cutoff(train_y, train_xgb)
    rule_scores = np.ones(len(val_y), dtype=np.float64)
    methods = {
        "rule_detectors_only": (rule_scores, 0.5, "all validation clusters emitted by train-configured rules are flagged"),
        "transparent_score_only": (val_transparent, transparent_cutoff, "cutoff selected on train clusters"),
        "full_xgboost_cluster_score": (val_xgb, xgb_cutoff, "saved M9 model; cutoff selected on train clusters"),
    }
    results = {}
    for name, (scores, cutoff, selection) in methods.items():
        metrics = binary_metrics(val_y, scores >= cutoff, scores).model_dump()
        results[name] = {**metrics, "cutoff": float(cutoff), "cutoff_selection": selection,
            "validation_clusters": int(len(val_y))}
    return {"command": "aml evaluation-artifacts", "period": "validation",
        "validation_only": True, "test_access": False,
        "label_definition": f"cluster positive when positive-edge fraction >= {settings.evaluation['cluster_tau']}",
        "train_clusters": int(len(train_y)), "validation_clusters": int(len(val_y)),
        "xgboost_artifact": str(tuned_path),
        "xgboost_hyperparameters_selected_on": "validation by saved M9 tuning output",
        "thresholds_selected_on": "train clusters only, except rule-detector presence baseline",
        "results": results}


def _posthoc_baseline(settings: Settings, split: TemporalSplit, system_report: dict[str, Any]) -> dict[str, Any]:
    test_start, test_end = split.bounds("test")
    test_frame = pq.read_table(settings.data.processed_dir / "transactions.parquet",
        filters=[("ts", ">=", test_start), ("ts", "<", test_end)]).to_pandas()
    if test_frame.empty:
        raise ValueError("test-period transactions are required for the post-hoc baseline")
    graph = build_graph(test_frame)
    baseline_report = _json(settings.output_dir / "m3" / "baseline.json")
    config = BaselineConfig.model_validate(baseline_report["selected"])
    result = baseline(graph, config)
    population = np.union1d(graph.src, graph.dst)
    positive_accounts = np.union1d(graph.src[graph.label == 1], graph.dst[graph.label == 1])
    account_metrics = binary_metrics(np.isin(population, positive_accounts).astype(np.int8),
        result.account_pred[population].astype(np.int8), result.account_scores[population]).model_dump()
    edge_metrics = binary_metrics(graph.label.astype(np.int8), result.edge_pred.astype(np.int8),
        result.edge_scores).model_dump()
    attempts_rows = pq.read_table(settings.data.processed_dir / "attempts.parquet",
        filters=[("t_start", ">=", test_start), ("t_start", "<", test_end)]).to_pylist()
    attempts = [Attempt.model_validate({key: row[key] for key in Attempt.model_fields}) for row in attempts_rows]
    alerted_edges = set(int(tx) for tx in graph.tx_id[result.edge_pred])
    alerted_accounts = set(int(node) for node in np.flatnonzero(result.account_pred))
    attempt_recall = attempt_coverage_report(attempts, alerted_edges, alerted_accounts)
    fractions, scores = [], []
    for cluster in result.clusters:
        positions = graph.positions_for_tx_ids(np.asarray(cluster.edge_ids, dtype=np.int64))
        if len(positions):
            fractions.append(float(graph.label[positions].mean()))
            scores.append(float(cluster.score))
    cluster_summary = cluster_precision_summary(fractions, scores, 1.0,
        taus=(settings.evaluation["cluster_tau"],), k=int(settings.evaluation["precision_k"]))
    return {"label": "Post-hoc comparison: train-tuned threshold baseline on test only",
        "purpose": "separate post-hoc comparator; not used to fit or alter the frozen graph system",
        "period": "test", "post_hoc": True, "test_only_input_rows": int(len(test_frame)),
        "train_tuned_baseline_config": config.model_dump(),
        "system_reference_from_saved_final_report": system_report["account"],
        "baseline": {"account": account_metrics, "edge": edge_metrics,
            "cluster": cluster_summary, "attempt_recall": attempt_recall},
        "frozen_system_config_changed": False, "frozen_system_report_changed": False}


def _markdown(payload: dict[str, Any]) -> str:
    lines = ["# Post-hoc baseline comparison", "", payload["label"], "",
        "This comparator uses the threshold configuration selected on train/validation. Only test-period transactions were passed to the baseline. It was computed after the frozen system report and does not change that report or the frozen system configuration.", "",
        "| Account metric | Frozen graph system | Post-hoc baseline |", "|---|---:|---:|"]
    for key, label in (("f1", "F1"), ("precision", "Precision"), ("recall", "Recall"), ("pr_auc", "PR-AUC")):
        system = payload["system_reference_from_saved_final_report"].get(key)
        baseline_value = payload["baseline"]["account"].get(key)
        fmt = lambda value: "—" if value is None else f"{value:.4f}"
        lines.append(f"| {label} | {fmt(system)} | {fmt(baseline_value)} |")
    lines.append("")
    return "\n".join(lines)


def _attempt_interval_markdown(payload: dict[str, Any]) -> str:
    lines = ["# Test attempt-recall confidence intervals", "", "Wilson score intervals, two-sided 95%, using saved M11 detected/attempt counts at 50% edge coverage. This generation step does not read test transactions.", "",
        "| Typology | Detected / attempts | Recall | Wilson 95% CI |", "|---|---:|---:|---:|"]
    for name, result in sorted(payload["typologies"].items()):
        ci = result.get("ci95")
        ratio = "Not covered" if result["status"] != "covered" else f"{result['detected']} / {result['attempts']}"
        recall = "Not covered" if result["status"] != "covered" else f"{result['recall']:.1%}"
        interval = "Not covered" if not ci else f"[{ci[0]:.1%}, {ci[1]:.1%}]"
        lines.append(f"| {name} | {ratio} | {recall} | {interval} |")
    return "\n".join(lines) + "\n"


def _ablation_markdown(payload: dict[str, Any]) -> str:
    lines = ["# Validation ablation", "", "All detector candidates and labels are restricted to train/validation periods. Transparent-score and XGBoost score cutoffs are selected on train clusters; the XGBoost model artifact was selected by M9 validation tuning.", "",
        "| Scoring method | F1 | Precision | Recall | PR-AUC | Cutoff |", "|---|---:|---:|---:|---:|---:|"]
    names = {"rule_detectors_only": "Rule detectors only", "transparent_score_only": "Transparent score only",
        "full_xgboost_cluster_score": "Full XGBoost cluster score"}
    for key, result in payload["results"].items():
        lines.append(f"| {names[key]} | {result['f1']:.1%} | {result['precision']:.1%} | {result['recall']:.1%} | {result['pr_auc']:.1%} | {result['cutoff']:.4g} |")
    lines.extend(["", f"Training clusters: {payload['train_clusters']:,}; validation clusters: {payload['validation_clusters']:,}. No test rows were read."])
    return "\n".join(lines) + "\n"


def run(settings: Settings) -> dict[str, Any]:
    """Regenerate validation/test-derived addenda from saved project artifacts."""
    final_path = settings.output_dir / "m11" / "final_report.json"
    final_bytes = final_path.read_bytes()
    system_report = json.loads(final_bytes)
    expected_hash = system_report.get("frozen_hash")
    current_hash = frozen_hash(settings)
    if not expected_hash or current_hash != expected_hash:
        raise RuntimeError("saved final report frozen hash does not match current frozen artifacts")

    split = TemporalSplit.model_validate_json((settings.data.processed_dir / "split.json").read_text())
    interval_payload = _attempt_intervals(system_report)
    output_dir = settings.output_dir / "evaluation"
    _write_json_md(output_dir, "attempt_recall_ci", interval_payload, _attempt_interval_markdown(interval_payload))

    hits = _hits(settings)
    ablation_payload = _ablation(settings, split, hits)
    _write_json_md(output_dir, "ablation", ablation_payload, _ablation_markdown(ablation_payload))

    prior_logging_disable = logging.root.manager.disable
    logging.disable(logging.WARNING)
    try:
        evasion_summary = run_evasion(settings)
    finally:
        logging.disable(prior_logging_disable)
    baseline_payload = _posthoc_baseline(settings, split, system_report)
    posthoc_dir = settings.output_dir / "posthoc"
    _write_json_md(posthoc_dir, "baseline_test_comparison", baseline_payload, _markdown(baseline_payload))

    if final_path.read_bytes() != final_bytes:
        raise RuntimeError("frozen system report changed during artifact generation")
    if frozen_hash(settings) != expected_hash:
        raise RuntimeError("frozen system artifacts changed during artifact generation")
    return {"command": "aml evaluation-artifacts", "frozen_hash": expected_hash,
        "attempt_recall_ci": str(output_dir / "attempt_recall_ci.json"),
        "ablation": str(output_dir / "ablation.json"),
        "evasion": evasion_summary,
        "posthoc_baseline_test": str(posthoc_dir / "baseline_test_comparison.json"),
        "final_system_report_unchanged": True, "frozen_hash_unchanged": True}
