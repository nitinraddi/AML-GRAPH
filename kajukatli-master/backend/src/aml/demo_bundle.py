"""Build a small, static excerpt from saved synthetic detector outputs."""
from __future__ import annotations

import json
import random
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pyarrow.dataset as ds

_MAX_CASES_PER_DETECTOR = 3
_MAX_TRANSFERS_PER_CASE = 20
_DETECTORS = (
    ("fan", "fan", "Fan-in / fan-out", {"fan_in": 1, "fan_out": 1}),
    ("scatter_gather", "scatter_gather", "Scatter-gather / gather-scatter",
        {"scatter_gather": 1, "gather_scatter": 1}),
    ("cycle", "cycle", "Time-respecting cycles", {"cycle": 3}),
    ("pass_through", "pass_through", "Rapid pass-through", {"pass_through": 3}),
)
_IBM_SOURCE = "https://github.com/IBM/AML-Data"
_IBM_LICENSE = "https://cdla.io/sharing-1-0/"


def _read_hit_lines(path: Path):
    with path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            line = line.strip().rstrip(",")
            if not line or line in {"[", "]"}:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid detector hit in {path} at line {line_number}") from exc
            if not isinstance(value, dict):
                raise ValueError(f"invalid detector hit in {path} at line {line_number}")
            yield value


def _sample_hits(path: Path, seed: int) -> list[dict[str, Any]]:
    randomizer = random.Random(seed)
    reservoirs: dict[str, list[dict[str, Any]]] = {}
    seen: dict[str, int] = {}
    for hit in _read_hit_lines(path):
        typology = hit.get("typology")
        edge_ids = hit.get("edge_ids")
        if not isinstance(typology, str) or not isinstance(edge_ids, list):
            raise ValueError(f"invalid detector hit fields in {path}")
        if not edge_ids or len(edge_ids) > _MAX_TRANSFERS_PER_CASE:
            continue
        reservoir = reservoirs.setdefault(typology, [])
        seen[typology] = seen.get(typology, 0) + 1
        if len(reservoir) < _MAX_CASES_PER_DETECTOR:
            reservoir.append(hit)
        else:
            index = randomizer.randrange(seen[typology])
            if index < _MAX_CASES_PER_DETECTOR:
                reservoir[index] = hit
    return [hit for typology in sorted(reservoirs) for hit in reservoirs[typology]]


def _select_cases(
    candidates: list[dict[str, Any]], required: dict[str, int], detector: str,
) -> list[dict[str, Any]]:
    by_type: dict[str, list[dict[str, Any]]] = {}
    for hit in candidates:
        by_type.setdefault(hit["typology"], []).append(hit)
    selected: list[dict[str, Any]] = []
    selected_ids: set[int] = set()
    for typology, count in required.items():
        options = by_type.get(typology, [])
        if len(options) < count:
            raise ValueError(f"{detector} has too few saved {typology} hits for the demo excerpt")
        selected.extend(options[:count])
        selected_ids.update(id(hit) for hit in options[:count])
    remaining = [
        hit for hit in candidates
        if id(hit) not in selected_ids and hit["typology"] in required
    ]
    selected.extend(remaining[:_MAX_CASES_PER_DETECTOR - len(selected)])
    if len(selected) != _MAX_CASES_PER_DETECTOR:
        raise ValueError(f"{detector} has too few compact saved cases for the demo excerpt")
    return selected


def build_demo_data(output_dir: Path, processed_dir: Path) -> dict[str, Any]:
    """Return a bounded demo payload from the saved hit exports and prepared Parquet."""
    accounts_path = processed_dir / "accounts.parquet"
    transactions_path = processed_dir / "transactions.parquet"
    if not accounts_path.is_file() or not transactions_path.is_file():
        raise FileNotFoundError("processed account/transaction Parquet is missing; run aml prepare first")

    detector_samples: list[tuple[str, str, list[dict[str, Any]]]] = []
    all_edge_ids: set[int] = set()
    all_account_ids: set[int] = set()
    for seed, (detector, folder, label, required) in enumerate(_DETECTORS, start=42):
        hits_path = output_dir / folder / "hits.json"
        if not hits_path.is_file():
            raise FileNotFoundError(f"saved {detector} detector output is missing: {hits_path}")
        candidates = _sample_hits(hits_path, seed)
        cases = _select_cases(candidates, required, detector)
        detector_samples.append((detector, label, cases))
        for hit in cases:
            all_edge_ids.update(int(value) for value in hit["edge_ids"])
            all_account_ids.update(int(value) for value in hit.get("accounts", []))

    transaction_table = ds.dataset(transactions_path, format="parquet").to_table(
        columns=["tx_id", "ts", "src", "dst", "amt_out", "cur_out"],
        filter=ds.field("tx_id").isin(sorted(all_edge_ids)),
    )
    transactions = {int(row["tx_id"]): row for row in transaction_table.to_pylist()}
    missing_edges = all_edge_ids.difference(transactions)
    if missing_edges:
        raise ValueError(f"saved detector hits reference missing transactions: {sorted(missing_edges)[:5]}")
    all_account_ids.update(int(row[key]) for row in transactions.values() for key in ("src", "dst"))

    accounts_table = ds.dataset(accounts_path, format="parquet").to_table(
        columns=["account_id", "bank", "account"],
        filter=ds.field("account_id").isin(sorted(all_account_ids)),
    )
    account_names = {
        int(row["account_id"]): f'{row["bank"]}:{row["account"]}'
        for row in accounts_table.to_pylist()
    }
    missing_accounts = all_account_ids.difference(account_names)
    if missing_accounts:
        raise ValueError(f"saved detector hits reference missing account IDs: {sorted(missing_accounts)[:5]}")

    detector_rows = []
    for detector, label, hits in detector_samples:
        cases = []
        for index, hit in enumerate(hits, start=1):
            edge_ids = [int(value) for value in hit["edge_ids"]]
            edges = sorted((transactions[edge_id] for edge_id in edge_ids),
                key=lambda row: (int(row["ts"]), int(row["tx_id"])))
            account_ids = sorted(
                {int(value) for value in hit.get("accounts", [])}
                | {int(row[key]) for row in edges for key in ("src", "dst")}
            )
            cases.append({
                "id": f"{detector}-{index:02d}",
                "typology": hit["typology"],
                "accounts": [account_names[value] for value in account_ids],
                "start": datetime.fromtimestamp(int(hit["t_start"]), timezone.utc).isoformat().replace("+00:00", "Z"),
                "end": datetime.fromtimestamp(int(hit["t_end"]), timezone.utc).isoformat().replace("+00:00", "Z"),
                "transfers": [{
                    "time": datetime.fromtimestamp(int(row["ts"]), timezone.utc).isoformat().replace("+00:00", "Z"),
                    "from": account_names[int(row["src"])],
                    "to": account_names[int(row["dst"])],
                    "amount": round(float(row["amt_out"]), 2),
                    "currency": str(row["cur_out"]),
                } for row in edges],
            })
        detector_rows.append({"id": detector, "label": label, "cases": cases})

    return {
        "banner": "Demo data excerpt - synthetic",
        "read_only": True,
        "source": "Saved detector outputs and a curated excerpt of IBM AML-Data (HI-Small).",
        "excerpt_notice": (
            "This file contains a small, transformed subset of synthetic benchmark data. "
            "It is provided under CDLA-Sharing-1.0; the excerpt is not the full dataset."
        ),
        "dataset": {
            "name": "IBM AML-Data (HI-Small)",
            "provider": "IBM",
            "synthetic": True,
            "source_url": _IBM_SOURCE,
            "license": "CDLA-Sharing-1.0",
            "license_url": _IBM_LICENSE,
        },
        "limits": {
            "cases_per_detector": _MAX_CASES_PER_DETECTOR,
            "transfers_per_case": _MAX_TRANSFERS_PER_CASE,
        },
        "detectors": detector_rows,
    }


def write_demo_bundle(output_dir: Path, processed_dir: Path, destination: Path) -> Path:
    data = build_demo_data(output_dir, processed_dir)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return destination
