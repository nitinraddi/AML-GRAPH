import json
from pathlib import Path
from threading import Thread
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from http.server import ThreadingHTTPServer

from aml.config import Settings
from aml.web import api_data, make_handler
import pandas as pd


def put_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def test_page_api_exposes_only_project_summaries(tmp_path):
    settings = Settings(output_dir=tmp_path / "outputs")
    put_json(settings.output_dir / "m1" / "checklist.json", {
        "transactions": 100, "accounts": 12, "positive_transactions": 4,
        "split": {"iso_boundaries": {"train": ["a", "b"], "validation": ["b", "c"]}}})
    put_json(settings.output_dir / "m8" / "report.json", {"clusters": 7})
    put_json(settings.output_dir / "m11" / "final_report.json", {
        "account": {"f1": .5, "recall": .6}, "attempts_by_start_period": 3,
        "test_transactions": 20, "positive_test_transactions": 2,
        "attempt_recall_edge_coverage_tau_0_5": {"CYCLE": {"status": "covered", "recall": .5}}})
    put_json(settings.output_dir / "fan" / "report.json", {"hits": 11})
    put_json(settings.output_dir / "evaluation" / "attempt_recall_ci.json", {
        "source": "outputs/m11/final_report.json", "period": "test", "confidence_level": .95,
        "typologies": {"CYCLE": {"ci95": [.2, .8]}}})
    put_json(settings.output_dir / "evaluation" / "ablation.json", {
        "period": "validation", "validation_only": True, "test_access": False,
        "results": {"rule_detectors_only": {"f1": .1}}})
    put_json(settings.output_dir / "posthoc" / "baseline_test_comparison.json", {
        "period": "test", "post_hoc": True, "frozen_system_config_changed": False,
        "frozen_system_report_changed": False, "label": "Post-hoc comparison",
        "test_only_input_rows": 20, "baseline": {"account": {"f1": .2}}})
    put_json(settings.output_dir / "evasion.json", {
        "period": "validation", "validation_only": True,
        "rows": [
            {"kind": "amount_split", "strength": .5, "main_attempt_recall": .2,
             "baseline_attempt_recall": .3},
            {"kind": "delays", "strength": .5, "main_attempt_recall": .1,
             "baseline_attempt_recall": .25}]})
    overview = api_data("overview", settings)
    assert overview == {"transactions": 100, "positive_transactions": 4, "accounts": 12,
        "detectors": 4, "ranked_clusters": 7, "test_report_available": True}
    evaluation = api_data("evaluation", settings)
    assert evaluation["account_f1"] == .5
    assert evaluation["attempt_recall_by_typology"]["CYCLE"]["recall"] == .5
    assert [point["kind"] for point in evaluation["evasion"]] == ["amount_split", "delays"]
    assert "Validation-only" in evaluation["evasion_scope"]
    assert "benign decoys" in evaluation["evasion_scope"]
    assert evaluation["attempt_recall_intervals"]["CYCLE"]["ci95"] == [.2, .8]
    assert evaluation["ablation"]["rule_detectors_only"]["f1"] == .1
    assert evaluation["posthoc_baseline"]["period"] == "test"
    encoded = json.dumps(overview)
    assert "account_id" not in encoded and "transaction_id" not in encoded


def test_evaluation_api_rejects_nonvalidation_or_unlabeled_artifacts(tmp_path):
    settings = Settings(output_dir=tmp_path / "outputs")
    put_json(settings.output_dir / "evaluation" / "ablation.json", {
        "period": "test", "validation_only": False, "test_access": True,
        "results": {"full_xgboost_cluster_score": {"f1": .9}}})
    put_json(settings.output_dir / "posthoc" / "baseline_test_comparison.json", {
        "period": "test", "post_hoc": False, "baseline": {"account": {"f1": .9}}})
    put_json(settings.output_dir / "evasion.json", {
        "period": "test", "validation_only": False,
        "rows": [{"kind": "combined", "strength": 1, "main_attempt_recall": 1,
                  "baseline_attempt_recall": 1}]})
    (settings.output_dir / "evasion.csv").write_text(
        "kind,strength,main_attempt_recall,baseline_attempt_recall\n"
        "combined,1,1,1\n", encoding="utf-8")

    evaluation = api_data("evaluation", settings)

    assert evaluation["ablation"] == {}
    assert evaluation["posthoc_baseline"] is None
    assert evaluation["evasion"] == []
    assert evaluation["evasion_scope"] is None


def test_web_server_serves_page_and_same_origin_api(tmp_path):
    settings = Settings(output_dir=tmp_path / "outputs")
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "index.html").write_text("<h1>AML Graph</h1>", encoding="utf-8")
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(settings, frontend))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        with urlopen(base + "/") as response:
            assert b"AML Graph" in response.read()
        with urlopen(base + "/api/overview") as response:
            assert json.loads(response.read())["transactions"] is None
        try:
            urlopen(base + "/api/nope")
        except HTTPError as error:
            assert error.code == 404
        else:
            raise AssertionError("unknown API route should return 404")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def add_analyst_artifacts(settings: Settings) -> None:
    accounts = ["A:one", "B:two"] + [f"B:other{index}" for index in range(25)]
    alert = {
        "rank": 1, "alert_id": "A-000001", "risk_score": 91.5,
        "primary_typology": "cycle", "secondary_typologies": [],
        "accounts": accounts, "edges": [
            {"tx_id": 11, "src": "A:one", "dst": "B:two", "ts": "2022-01-01T00:00:00Z",
             "amount": 12.5, "currency": "USD"},
        ],
        "time_span": {"start": "2022-01-01T00:00:00Z", "end": "2022-01-01T01:00:00Z"},
        "summary": "A deterministic template summary.", "summary_source": "template",
        "evidence": {"hits": [{"typology": "cycle", "meta": {"length": 3}}]},
    }
    other_alert = {
        **alert, "rank": 2, "alert_id": "A-000002", "risk_score": 35.0,
        "primary_typology": "fan_out",
    }
    put_json(settings.output_dir / "m8" / "alerts.json", [alert, other_alert])
    put_json(settings.output_dir / "m11" / "final_report.json", {
        "cluster": {"0.5": {"alert_count": 10, "precision": 0.4}},
    })
    settings.data.processed_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({
        "account_id": [1, 2], "bank": ["A", "B"], "account": ["one", "two"],
    }).to_parquet(settings.data.processed_dir / "accounts.parquet")
    pd.DataFrame({
        "tx_id": [11, 12, 13], "ts": [1000, 2000, 3000],
        "src": [1, 2, 1], "dst": [2, 1, 2],
        "amt_out": [12.5, 8.0, 9.0], "cur_out": ["USD", "USD", "EUR"],
        "label": [1, 0, 0],
    }).to_parquet(settings.data.processed_dir / "transactions.parquet")


def test_analyst_endpoints_queue_case_report_and_account_history(tmp_path):
    settings = Settings(output_dir=tmp_path / "outputs",
        data={"processed_dir": tmp_path / "processed"},
        detectors={"cycle": {"max_length": 5, "window_seconds": 3600}})
    add_analyst_artifacts(settings)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(settings))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with urlopen(base + "/api/queue?typology=cycle&min_score=80") as response:
            queue = json.loads(response.read())
        assert queue["total"] == 1
        assert queue["alerts_per_true_hit"]["alerts_per_true_hit"] == 2.5
        assert queue["alerts"][0]["rank"] == 1
        assert queue["alerts"][0]["decision"] == "pending"

        with urlopen(base + "/api/case?rank=1") as response:
            case = json.loads(response.read())
        assert case["summary"] == "A deterministic template summary."
        assert case["matched_conditions"][0].startswith("Cycle:")
        assert len(case["accounts"]) == 27

        with urlopen(base + "/api/case-report?rank=1") as response:
            report = json.loads(response.read())
        assert len(report["accounts"]) == 25
        assert report["omitted_accounts"] == 2
        assert len(report["transfers"]) == 1

        with urlopen(base + "/api/account-history?account=A%3Aone&rank=1") as response:
            history = json.loads(response.read())
        matched = next(row for row in history["history"] if row["id"] == 11)
        assert history["total"] == 3
        assert matched["matched_pattern"] is True
        assert matched["direction"] == "outgoing"

        request = Request(base + "/api/case/decision",
            data=json.dumps({"rank": 1, "decision": "accepted", "note": "Reviewed."}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urlopen(request) as response:
            decision = json.loads(response.read())
        assert decision["decision"] == "accepted"
        assert json.loads((settings.output_dir / "review" / "decisions.json").read_text())["1"]["note"] == "Reviewed."

        with urlopen(base + "/api/queue?typology=cycle&min_score=80") as response:
            updated_queue = json.loads(response.read())
        assert updated_queue["alerts"][0]["decision"] == "accepted"
        assert updated_queue["alerts"][0]["note"] == "Reviewed."
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_case_decision_endpoint_rejects_invalid_decisions(tmp_path):
    settings = Settings(output_dir=tmp_path / "outputs",
        data={"processed_dir": tmp_path / "processed"})
    add_analyst_artifacts(settings)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(settings))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        for body, expected_status in [
            ({"rank": 1, "decision": "maybe", "note": ""}, 400),
            ({"rank": 999, "decision": "accepted", "note": ""}, 404),
        ]:
            request = Request(base + "/api/case/decision", data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
            try:
                urlopen(request)
            except HTTPError as error:
                assert error.code == expected_status
                assert "error" in json.loads(error.read())
            else:
                raise AssertionError(f"invalid decision request should return {expected_status}")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_investigation_api_returns_real_hit_summaries_and_exact_subgraph(tmp_path):
    settings = Settings(output_dir=tmp_path / "outputs", data={"processed_dir": tmp_path / "processed"},
        detectors={"fan": {"k": 2}})
    settings.data.processed_dir.mkdir(parents=True)
    hits = [
        {"typology": "fan_in", "accounts": [1, 2, 3], "edge_ids": [10, 11],
         "t_start": 100, "t_end": 200, "total_value": 75.0,
         "meta": {"hub": 1, "distinct_counterparties": 2, "value_currency": "USD", "fx_missing": False}},
        {"typology": "fan_out", "accounts": [1, 4], "edge_ids": [12],
         "t_start": 300, "t_end": 300, "total_value": 12.0, "meta": {"hub": 1}},
    ]
    path = settings.output_dir / "fan" / "hits.json"
    path.parent.mkdir(parents=True)
    path.write_text("[\n" + ",\n".join(json.dumps(hit) for hit in hits) + "\n]\n", encoding="utf-8")
    pd.DataFrame({"tx_id": [10, 11, 12, 99], "ts": [200, 100, 300, 150],
        "src": [2, 3, 1, 8], "dst": [1, 1, 4, 9], "amt_out": [25., 50., 12., 1.],
        "cur_out": ["USD"] * 4, "label": [1, 0, 1, 0]}).to_parquet(settings.data.processed_dir / "transactions.parquet")
    pd.DataFrame({"account_id": [1, 2, 3, 4, 8, 9], "bank": ["A", "B", "C", "D", "X", "Y"]}).to_parquet(settings.data.processed_dir / "accounts.parquet")

    cases = api_data("investigation/cases", settings, {"detector": ["fan"], "offset": ["0"], "limit": ["1"]})
    assert cases["total"] == 2 and cases["has_more"] is True
    assert cases["cases"][0]["edge_count"] == 2
    subgraph = api_data("investigation/case", settings, {"detector": ["fan"], "id": ["0"]})
    assert {edge["id"] for edge in subgraph["edges"]} == {10, 11}
    assert [(edge["src"], edge["dst"]) for edge in subgraph["edges"]] == [(3, 1), (2, 1)]
    assert "2 distinct counterparties" in subgraph["conditions"][0]
    assert [node["id"] for node in subgraph["nodes"]] == [1, 2, 3]
    assert set(subgraph["currencies"]) == {"USD"}
    assert all(edge["id"] != 99 for edge in subgraph["edges"])


def test_investigation_account_finds_cases_with_case_endpoint_ids(tmp_path):
    settings = Settings(output_dir=tmp_path / "outputs")
    fan_path = settings.output_dir / "fan" / "hits.json"
    fan_hits = [
        {"typology": "unrelated", "accounts": [7]},
        {"typology": "fan_out", "accounts": ["7", 8], "edge_ids": [1, 2],
         "t_start": 10, "t_end": 20, "total_value": 50, "meta": {"value_currency": "USD"}},
    ]
    fan_path.parent.mkdir(parents=True)
    fan_path.write_text("[\n" + ",\n".join(json.dumps(hit) for hit in fan_hits) + "\n]\n", encoding="utf-8")
    cycle_hits = [
        {"typology": "cycle", "accounts": [7, 9], "edge_ids": [3],
         "t_start": 30, "t_end": 40, "total_value": 25, "meta": {"value_currency": "EUR"}},
    ]
    cycle_path = settings.output_dir / "cycle" / "hits.json"
    cycle_path.parent.mkdir(parents=True)
    cycle_path.write_text("[\n" + ",\n".join(json.dumps(hit) for hit in cycle_hits) + "\n]\n", encoding="utf-8")

    result = api_data("investigation/account", settings, {"id": ["7"]})
    fan_cases = api_data("investigation/cases", settings, {"detector": ["fan"], "offset": ["0"], "limit": ["30"]})
    cycle_cases = api_data("investigation/cases", settings, {"detector": ["cycle"], "offset": ["0"], "limit": ["30"]})
    assert result["account"] == 7 and result["total"] == 2 and result["truncated"] is False
    assert [(case["detector"], case["id"]) for case in result["cases"]] == [("fan", 1), ("cycle", 0)]
    assert result["cases"][0]["id"] == fan_cases["cases"][0]["id"]
    assert result["cases"][1]["id"] == cycle_cases["cases"][0]["id"]
    assert result["cases"][0]["currency"] == "USD"


def test_investigation_account_handles_missing_invalid_and_unmatched_ids(tmp_path):
    settings = Settings(output_dir=tmp_path / "outputs")
    missing = api_data("investigation/account", settings, {})
    invalid = api_data("investigation/account", settings, {"id": ["7x"]})
    absent = api_data("investigation/account", settings, {"id": ["7"]})
    assert missing == {"error": "invalid account id", "cases": [], "total": 0}
    assert invalid == {"error": "invalid account id", "cases": [], "total": 0}
    assert absent == {"account": 7, "cases": [], "total": 0, "truncated": False}


def test_investigation_account_caps_results_and_only_reads_hits(tmp_path):
    settings = Settings(output_dir=tmp_path / "outputs")
    hits_path = settings.output_dir / "fan" / "hits.json"
    hits = [{"typology": "fan_out", "accounts": [12], "edge_ids": [index],
             "t_start": index, "t_end": index, "total_value": index, "meta": {}}
            for index in range(101)]
    hits_path.parent.mkdir(parents=True)
    hits_path.write_text("[\n" + ",\n".join(json.dumps(hit) for hit in hits) + "\n]\n", encoding="utf-8")
    before = {path: path.read_bytes() for path in settings.output_dir.rglob("*") if path.is_file()}

    result = api_data("investigation/account", settings, {"id": ["12"]})

    after = {path: path.read_bytes() for path in settings.output_dir.rglob("*") if path.is_file()}
    assert result["total"] == 101 and result["truncated"] is True
    assert len(result["cases"]) == 100
    assert [case["id"] for case in result["cases"]] == list(range(100))
    assert after == before
