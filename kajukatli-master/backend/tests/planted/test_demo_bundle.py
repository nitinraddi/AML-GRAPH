import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from aml.demo_bundle import build_demo_data, write_demo_bundle


def _write_hits(output_dir: Path, folder: str, hits: list[dict]) -> None:
    path = output_dir / folder / "hits.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[\n" + ",\n".join(json.dumps(hit) for hit in hits) + "\n]\n",
        encoding="utf-8")


def _demo_sources(tmp_path: Path) -> tuple[Path, Path, list[dict]]:
    output_dir = tmp_path / "outputs"
    processed_dir = tmp_path / "processed"
    processed_dir.mkdir()
    definitions = [
        ("fan", ["fan_in", "fan_out", "fan_in"]),
        ("scatter_gather", ["scatter_gather", "gather_scatter", "gather_scatter"]),
        ("cycle", ["cycle", "cycle", "cycle"]),
        ("pass_through", ["pass_through", "pass_through", "pass_through"]),
    ]
    hits = []
    transactions = []
    accounts = []
    tx_id = 1
    for folder, typologies in definitions:
        detector_hits = []
        for typology in typologies:
            src, dst = tx_id * 2, tx_id * 2 + 1
            detector_hits.append({
                "typology": typology,
                "accounts": [src] if folder == "pass_through" and tx_id == 10 else [src, dst],
                "edge_ids": [tx_id],
                "t_start": 1_700_000_000 + tx_id,
                "t_end": 1_700_000_000 + tx_id,
                "meta": {},
            })
            transactions.append({
                "tx_id": tx_id, "ts": 1_700_000_000 + tx_id,
                "src": src, "dst": dst, "amt_out": float(tx_id),
                "cur_out": "USD",
            })
            accounts.extend([
                {"account_id": src, "bank": "bank", "account": f"a{src}"},
                {"account_id": dst, "bank": "bank", "account": f"a{dst}"},
            ])
            tx_id += 1
        _write_hits(output_dir, folder, detector_hits)
        hits.extend(detector_hits)
    pq.write_table(pa.Table.from_pylist(accounts), processed_dir / "accounts.parquet")
    pq.write_table(pa.Table.from_pylist(transactions), processed_dir / "transactions.parquet")
    return output_dir, processed_dir, hits


def test_static_demo_is_bounded_read_only_and_credited(tmp_path):
    output_dir, processed_dir, _ = _demo_sources(tmp_path)

    data = build_demo_data(output_dir, processed_dir)

    assert build_demo_data(output_dir, processed_dir) == data
    assert data["banner"] == "Demo data excerpt - synthetic"
    assert data["read_only"] is True
    assert data["dataset"]["license"] == "CDLA-Sharing-1.0"
    assert data["dataset"]["source_url"] == "https://github.com/IBM/AML-Data"
    assert len(data["detectors"]) == 4
    assert all(len(detector["cases"]) == 3 for detector in data["detectors"])
    assert all(len(case["transfers"]) <= 20
        for detector in data["detectors"] for case in detector["cases"])
    assert {case["typology"] for case in data["detectors"][0]["cases"]} == {"fan_in", "fan_out"}
    assert {"scatter_gather", "gather_scatter"} <= {
        case["typology"] for case in data["detectors"][1]["cases"]
    }
    assert set(data["detectors"][0]["cases"][0]["transfers"][0]) == {
        "time", "from", "to", "amount", "currency",
    }
    assert all(
        {transfer["from"], transfer["to"]} <= set(case["accounts"])
        for detector in data["detectors"] for case in detector["cases"]
        for transfer in case["transfers"]
    )


def test_static_demo_writer_outputs_only_the_curated_bundle(tmp_path):
    output_dir, processed_dir, _ = _demo_sources(tmp_path)
    destination = tmp_path / "docs" / "demo-data.json"

    result = write_demo_bundle(output_dir, processed_dir, destination)

    saved = json.loads(destination.read_text(encoding="utf-8"))
    assert result == destination
    assert len(saved["detectors"]) == 4
    assert destination.stat().st_size < 20_000


def test_checked_in_demo_artifact_is_bounded_and_attributed():
    project_root = Path(__file__).resolve().parents[3]
    bundle_path = project_root / "docs" / "demo-data.json"
    data = json.loads(bundle_path.read_text(encoding="utf-8"))

    assert data["banner"] == "Demo data excerpt - synthetic"
    assert data["read_only"] is True
    assert data["dataset"]["license"] == "CDLA-Sharing-1.0"
    assert len(data["detectors"]) == 4
    assert all(1 <= len(detector["cases"]) <= 3 for detector in data["detectors"])
    assert all(1 <= len(case["transfers"]) <= 20
        for detector in data["detectors"] for case in detector["cases"])
    assert bundle_path.stat().st_size < 30_000
