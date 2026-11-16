"""Local HTTP surface joining the static frontend to saved AML artifacts."""
from __future__ import annotations

import heapq
import json
import mimetypes
import threading
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from aml.config import Settings

PROJECT_ROOT = Path(__file__).resolve().parents[3]
FRONTEND_FILES = {"index.html", "approach.html", "patterns.html", "evaluation.html", "investigation.html",
    "queue.html", "case.html", "case-report.html", "site-pages.css", "site-pages.js",
    "investigation.css", "investigation.js", "analyst.css", "queue.js", "case.js", "case-report.js", "README.md"}

_DETECTORS = {
    "fan": ("fan", "Fan in / fan out", {"fan_in", "fan_out"}),
    "scatter": ("scatter_gather", "Scatter & gather", {"scatter_gather", "gather_scatter"}),
    "cycle": ("cycle", "Time-respecting cycles", {"cycle"}),
    "pass": ("pass_through", "Rapid pass-through", {"pass_through"}),
}
_ALERT_TYPOLOGIES = ("fan_in", "fan_out", "scatter_gather", "gather_scatter", "cycle", "pass_through")
_DECISION_LOCK = threading.RLock()
_DECISIONS_FILENAME = "decisions.json"
_MAX_NOTE_LENGTH = 2000
_MAX_HISTORY_ROWS = 200
_MAX_CASE_EDGES = 500
_MAX_REPORT_EDGES = 25


def _hit_rows(path: Path):
    """Yield individual JSON array items; hit files can be hundreds of MB."""
    try:
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                line = line.strip().rstrip(",")
                if not line or line in {"[", "]"}:
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    yield value
    except OSError:
        return


def _investigation(endpoint: str, query: dict[str, list[str]], settings: Settings) -> dict[str, Any] | None:
    import pyarrow.dataset as ds

    if endpoint == "investigation/account":
        raw_account = query.get("id", [""])[0]
        try:
            account_id = int(raw_account)
        except (TypeError, ValueError):
            return {"error": "invalid account id", "cases": [], "total": 0}
        matches = []
        for detector, (folder, _, typologies) in _DETECTORS.items():
            for raw_index, hit in enumerate(_hit_rows(settings.output_dir / folder / "hits.json")):
                if hit.get("typology") not in typologies:
                    continue
                if account_id not in (int(value) for value in hit.get("accounts", [])):
                    continue
                matches.append({"detector": detector, "id": raw_index, "typology": hit["typology"],
                    "edge_count": len(hit.get("edge_ids", [])), "account_count": len(hit.get("accounts", [])),
                    "t_start": hit.get("t_start"), "t_end": hit.get("t_end"),
                    "total_value": hit.get("total_value"),
                    "currency": hit.get("meta", {}).get("value_currency")})
        total = len(matches)
        return {"account": account_id, "cases": matches[:100], "total": total, "truncated": total > 100}

    if endpoint == "investigation/detectors":
        return {"detectors": [{"id": key, "label": label,
            "available": (settings.output_dir / folder / "hits.json").is_file(),
            "hit_count": int((_read_json(settings.output_dir / folder / "report.json") or {}).get("hits", 0))}
            for key, (folder, label, _) in _DETECTORS.items()]}
    if endpoint == "investigation/cases":
        detector = query.get("detector", [""])[0]
        if detector not in _DETECTORS:
            return {"error": "unknown detector", "cases": [], "total": 0}
        offset = max(0, int(query.get("offset", ["0"])[0]))
        limit = min(100, max(1, int(query.get("limit", ["30"])[0])))
        folder, _, typologies = _DETECTORS[detector]
        rows = []
        total = 0
        for raw_index, hit in enumerate(_hit_rows(settings.output_dir / folder / "hits.json")):
            if hit.get("typology") not in typologies:
                continue
            if total >= offset and len(rows) < limit:
                rows.append({"id": raw_index, "typology": hit["typology"],
                    "edge_count": len(hit.get("edge_ids", [])), "account_count": len(hit.get("accounts", [])),
                    "t_start": hit.get("t_start"), "t_end": hit.get("t_end"),
                    "total_value": hit.get("total_value"), "currency": hit.get("meta", {}).get("value_currency"),
                    "meta": hit.get("meta", {})})
            total += 1
        return {"detector": detector, "cases": rows, "offset": offset, "limit": limit, "total": total,
            "has_more": offset + len(rows) < total}
    if endpoint == "investigation/case":
        detector = query.get("detector", [""])[0]
        if detector not in _DETECTORS:
            return {"error": "unknown detector"}
        case_id = int(query.get("id", ["-1"])[0])
        folder, _, _ = _DETECTORS[detector]
        hit = next((row for index, row in enumerate(_hit_rows(settings.output_dir / folder / "hits.json")) if index == case_id), None)
        if hit is None:
            return {"error": "case not found"}
        edge_ids = [int(value) for value in hit.get("edge_ids", [])]
        max_edges = 5000
        omitted = max(0, len(edge_ids) - max_edges)
        edge_ids = edge_ids[:max_edges]
        tx_path = settings.data.processed_dir / "transactions.parquet"
        account_path = settings.data.processed_dir / "accounts.parquet"
        if not tx_path.is_file() or not account_path.is_file():
            return {"error": "processed transaction/account Parquet is missing; run aml prepare first"}
        tx = ds.dataset(tx_path, format="parquet").to_table(
            columns=["tx_id", "ts", "src", "dst", "amt_out", "cur_out", "label"],
            filter=ds.field("tx_id").isin(edge_ids)).to_pydict()
        order = sorted(range(len(tx["tx_id"])), key=lambda i: (tx["ts"][i], tx["tx_id"][i]))
        accounts = [int(value) for value in hit.get("accounts", [])]
        account_rows = ds.dataset(account_path, format="parquet").to_table(
            columns=["account_id", "bank"], filter=ds.field("account_id").isin(accounts)).to_pydict()
        banks = dict(zip(account_rows["account_id"], account_rows["bank"]))
        currencies = sorted({str(tx["cur_out"][i]) for i in order})
        actual_edges = [{"id": int(tx["tx_id"][i]), "src": int(tx["src"][i]), "dst": int(tx["dst"][i]),
            "amount": float(tx["amt_out"][i]), "currency": str(tx["cur_out"][i]),
            "ts": int(tx["ts"][i]), "label": int(tx["label"][i])} for i in order]
        config_key = {"fan": "fan", "scatter": "scatter_gather", "cycle": "cycle", "pass": "pass_through"}[detector]
        cfg = settings.detectors.get(config_key, {})
        meta = hit.get("meta", {})
        conditions: list[str] = []
        if detector == "fan":
            conditions.append(f"Hub {meta.get('hub')} connects {meta.get('distinct_counterparties')} distinct counterparties; configured minimum is {cfg.get('k')} within {meta.get('window_seconds')} seconds.")
        elif detector == "scatter":
            stages = meta.get("stages", {})
            placement, integration = stages.get("placement", []), stages.get("integration", [])
            if hit.get("typology") == "gather_scatter":
                hub = meta.get("hub")
                incoming = [edge for edge in actual_edges if edge["dst"] == hub]
                outgoing = [edge for edge in actual_edges if edge["src"] == hub]
                conditions.append(f"Hub {hub} matched {len({edge['src'] for edge in incoming})} distinct incoming sources (minimum {cfg.get('m_in')}) and {len({edge['dst'] for edge in outgoing})} distinct outgoing destinations (minimum {cfg.get('m_out')}).")
            else:
                conditions.append(f"Direct paths link source {meta.get('source')} to sink {meta.get('sink')} through {len(meta.get('middle_accounts', []))} distinct middle accounts (minimum {cfg.get('m')}).")
            conditions.append(f"Detector window is {cfg.get('window_seconds')} seconds; matched placement/integration stages contain {len(placement)}/{len(integration)} transfers. Configured FX amount ratio is [{cfg.get('rho_lo')}, {1 + float(cfg.get('eps', 0))}].")
        elif detector == "cycle":
            conditions.append(f"Time-ordered cycle length {meta.get('length', len(edge_ids))} is within the configured maximum {cfg.get('max_length')}; candidate span is {hit.get('t_end', 0) - hit.get('t_start', 0)} seconds (window {cfg.get('window_seconds')}).")
            conditions.append(f"Each comparable FX-normalized hop is checked against ratio [{cfg.get('rho_lo')}, {1 + float(cfg.get('eps', 0))}]; fx_missing={bool(meta.get('fx_missing'))}.")
        else:
            conditions.append(f"Matched path contains {meta.get('links')} links (configured minimum {cfg.get('min_links')}) with {meta.get('likely_mules', [])} as intermediate accounts; maximum dwell per hop is {cfg.get('dwell_seconds')} seconds.")
            conditions.append(f"Comparable FX-normalized hop ratios are bounded by [{cfg.get('rho_lo')}, {1 + float(cfg.get('eps', 0))}]; fx_missing={bool(meta.get('fx_missing'))}.")
        if meta.get("fx_missing"):
            conditions.append("At least one FX conversion was unavailable; the detector marks the amount comparison as skipped for that hop.")
        return {"detector": detector, "case": {"id": case_id, **hit, "omitted_edges": omitted},
            "nodes": [{"id": account, "bank": banks.get(account)} for account in accounts],
            "edges": actual_edges, "conditions": conditions, "currencies": currencies}
    return None


def _read_json(path: Path) -> dict[str, Any] | list[Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, (dict, list)) else None
    except (OSError, json.JSONDecodeError):
        return None


def _saved_alerts(settings: Settings) -> list[dict[str, Any]]:
    value = _read_json(settings.output_dir / "m8" / "alerts.json")
    if not isinstance(value, list):
        raise FileNotFoundError("saved ranked alert export is missing; run aml cluster first")
    return [alert for alert in value if isinstance(alert, dict)]


def _decisions_path(settings: Settings) -> Path:
    return settings.output_dir / "review" / _DECISIONS_FILENAME


def _read_decisions(settings: Settings) -> dict[str, dict[str, Any]]:
    path = _decisions_path(settings)
    if not path.exists():
        return {}
    with _DECISION_LOCK:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"could not read case decisions from {path}: {exc}") from exc
    if not isinstance(value, dict) or any(
        not isinstance(rank, str) or not isinstance(item, dict)
        or not isinstance(item.get("decision"), str)
        or item.get("decision") not in {"accepted", "dismissed"}
        or not isinstance(item.get("note", ""), str)
        for rank, item in value.items()
    ):
        raise ValueError(f"case decisions file has an invalid format: {path}")
    return value


def _decision_for(settings: Settings, rank: int) -> dict[str, Any]:
    return _read_decisions(settings).get(str(rank), {
        "decision": "pending", "note": "", "updated_at": None,
    })


def _alerts_per_true_hit(settings: Settings) -> dict[str, Any]:
    report = _read_json(settings.output_dir / "m11" / "final_report.json")
    metrics = report.get("cluster", {}).get("0.5", {}) if isinstance(report, dict) else {}
    alert_count = metrics.get("alert_count")
    precision = metrics.get("precision")
    if not isinstance(alert_count, int) or not isinstance(precision, (int, float)) or precision <= 0:
        return {"available": False, "alerts_per_true_hit": None, "true_hits": None, "alerts": None}
    true_hits = round(alert_count * precision)
    return {"available": true_hits > 0, "alerts_per_true_hit": alert_count / true_hits if true_hits else None,
        "true_hits": true_hits, "alerts": alert_count, "period": "test",
        "positive_edge_fraction_threshold": 0.5}


def _queue(settings: Settings, query: dict[str, list[str]]) -> dict[str, Any]:
    alerts = sorted(_saved_alerts(settings), key=lambda alert: int(alert.get("rank", 0)))
    raw_typology = query.get("typology", [""])[0].strip()
    typology = raw_typology if raw_typology in _ALERT_TYPOLOGIES else ""
    try:
        min_score = float(query.get("min_score", ["0"])[0])
        offset = max(0, int(query.get("offset", ["0"])[0]))
        limit = min(100, max(1, int(query.get("limit", ["100"])[0])))
    except ValueError as exc:
        raise ValueError("invalid queue filter or pagination value") from exc
    if not 0 <= min_score <= 100:
        raise ValueError("min_score must be between 0 and 100")
    decisions = _read_decisions(settings)
    filtered = []
    for alert in alerts:
        score = float(alert.get("risk_score", 0))
        types = sorted({str(alert.get("primary_typology", "")), *alert.get("secondary_typologies", [])})
        if typology and typology not in types:
            continue
        if score < min_score:
            continue
        rank = int(alert["rank"])
        decision = decisions.get(str(rank), {"decision": "pending", "note": "", "updated_at": None})
        filtered.append({
            "rank": rank, "score": score, "typology": alert.get("primary_typology"),
            "typologies": types, "account_count": len(alert.get("accounts", [])),
            "transfer_count": len(alert.get("edges", [])),
            "start": alert.get("time_span", {}).get("start"), "end": alert.get("time_span", {}).get("end"),
            "summary": alert.get("summary", ""), "decision": decision.get("decision", "pending"),
            "note": decision.get("note", ""),
        })
    return {
        "available": True, "source": "outputs/m8/alerts.json",
        "exported_alerts": len(alerts), "total": len(filtered), "offset": offset, "limit": limit,
        "has_more": offset + limit < len(filtered), "typologies": list(_ALERT_TYPOLOGIES),
        "alerts_per_true_hit": _alerts_per_true_hit(settings),
        "alerts": filtered[offset:offset + limit],
    }


def _matched_conditions(alert: dict[str, Any], settings: Settings) -> list[str]:
    labels = {
        "fan_in": "Fan-in", "fan_out": "Fan-out", "scatter_gather": "Scatter-gather",
        "gather_scatter": "Gather-scatter", "cycle": "Cycle", "pass_through": "Pass-through",
    }
    configurations = {
        "fan_in": settings.detectors.get("fan", {}),
        "fan_out": settings.detectors.get("fan", {}),
        "scatter_gather": settings.detectors.get("scatter_gather", {}),
        "gather_scatter": settings.detectors.get("scatter_gather", {}),
        "cycle": settings.detectors.get("cycle", {}),
        "pass_through": settings.detectors.get("pass_through", {}),
    }
    conditions = []
    evidence_hits = alert.get("evidence", {}).get("hits", [])
    for hit in evidence_hits:
        typology = hit.get("typology")
        if typology not in labels:
            continue
        meta = hit.get("meta", {})
        config = configurations[typology]
        if typology in {"fan_in", "fan_out"}:
            text = (f"Hub {meta.get('hub')} links {meta.get('distinct_counterparties')} distinct counterparties "
                f"(minimum {config.get('k')}) within {meta.get('window_seconds')} seconds.")
        elif typology in {"scatter_gather", "gather_scatter"}:
            stages = meta.get("stages", {})
            placement = len(stages.get("placement", []))
            integration = len(stages.get("integration", []))
            if typology == "scatter_gather":
                text = (f"Direct paths link source {meta.get('source')} to sink {meta.get('sink')} through "
                    f"{len(meta.get('middle_accounts', []))} middle accounts (minimum {config.get('m')}); "
                    f"placement/integration stages contain {placement}/{integration} transfers within "
                    f"{config.get('window_seconds')} seconds.")
            else:
                text = (f"Gather-scatter hub {meta.get('hub')} has {placement}/{integration} transfers in "
                    f"its incoming/outgoing stages (minimum counterparties {config.get('m_in')}/"
                    f"{config.get('m_out')}) within {config.get('window_seconds')} seconds.")
        elif typology == "cycle":
            text = (f"Time-ordered cycle length {meta.get('length')} is within maximum "
                f"{config.get('max_length')}; configured window is {config.get('window_seconds')} seconds.")
        else:
            text = (f"Matched path contains {meta.get('links')} links (minimum {config.get('min_links')}); "
                f"maximum dwell per hop is {config.get('dwell_seconds')} seconds.")
        conditions.append(f"{labels[typology]}: {text}")
        if meta.get("fx_missing"):
            conditions.append(f"{labels[typology]}: an FX conversion was unavailable for at least one hop.")
    return conditions


def _case_payload(settings: Settings, rank: int, edge_limit: int,
        account_limit: int | None = None) -> dict[str, Any]:
    alert = next((row for row in _saved_alerts(settings) if int(row.get("rank", 0)) == rank), None)
    if alert is None:
        raise FileNotFoundError(f"saved alert rank {rank} was not found")
    if alert.get("summary_source") != "template":
        raise ValueError("case report requires a saved deterministic template narrative")
    edges = alert.get("edges", [])
    accounts = alert.get("accounts", [])
    account_limit = len(accounts) if account_limit is None else account_limit
    decision = _decision_for(settings, rank)
    return {
        "rank": rank, "alert_id": alert.get("alert_id"), "score": alert.get("risk_score"),
        "typology": alert.get("primary_typology"),
        "secondary_typologies": alert.get("secondary_typologies", []),
        "accounts": accounts[:account_limit], "account_count": len(accounts),
        "omitted_accounts": max(0, len(accounts) - account_limit),
        "time_range": alert.get("time_span"), "summary": alert.get("summary"),
        "summary_source": alert.get("summary_source"),
        "matched_conditions": _matched_conditions(alert, settings),
        "transfers": edges[:edge_limit], "omitted_transfers": max(0, len(edges) - edge_limit),
        "pattern_edge_ids": [int(edge["tx_id"]) for edge in edges],
        "decision": decision,
    }


def _case_from_query(settings: Settings, query: dict[str, list[str]], edge_limit: int,
        account_limit: int | None = None) -> dict[str, Any]:
    try:
        rank = int(query.get("rank", [""])[0])
    except ValueError as exc:
        raise ValueError("rank must be a positive integer") from exc
    if rank < 1:
        raise ValueError("rank must be a positive integer")
    return _case_payload(settings, rank, edge_limit, account_limit)


def _account_history(settings: Settings, query: dict[str, list[str]]) -> dict[str, Any]:
    import pyarrow.dataset as ds

    account_value = query.get("account", [""])[0].strip()
    if not account_value:
        raise ValueError("account is required")
    accounts_path = settings.data.processed_dir / "accounts.parquet"
    transactions_path = settings.data.processed_dir / "transactions.parquet"
    if not accounts_path.is_file() or not transactions_path.is_file():
        raise FileNotFoundError("processed transaction/account Parquet is missing; run aml prepare first")
    account_ds = ds.dataset(accounts_path, format="parquet")
    if account_value.isdecimal():
        account_filter = ds.field("account_id") == int(account_value)
    elif ":" in account_value:
        bank, account = account_value.split(":", 1)
        account_filter = (ds.field("bank") == bank) & (ds.field("account") == account)
    else:
        raise ValueError("account must be a numeric ID or a saved bank:account name")
    account_rows = account_ds.to_table(columns=["account_id", "bank", "account"], filter=account_filter).to_pylist()
    if not account_rows:
        raise FileNotFoundError(f"account {account_value} was not found")
    account = account_rows[0]
    account_id = int(account["account_id"])

    rank_values = query.get("rank", [])
    pattern_edge_ids: set[int] = set()
    if rank_values:
        case = _case_from_query(settings, {"rank": rank_values}, _MAX_CASE_EDGES)
        pattern_edge_ids = set(case["pattern_edge_ids"])
    direction = (ds.field("src") == account_id) | (ds.field("dst") == account_id)
    dataset = ds.dataset(transactions_path, format="parquet")
    scanner = dataset.scanner(columns=["tx_id", "ts", "src", "dst", "amt_out", "cur_out", "label"],
        filter=direction, batch_size=10_000)
    recent: list[tuple[int, int, dict[str, Any]]] = []
    total = 0
    for batch in scanner.to_batches():
        for row in batch.to_pylist():
            total += 1
            item = (int(row["ts"]), int(row["tx_id"]), row)
            if len(recent) < _MAX_HISTORY_ROWS:
                heapq.heappush(recent, item)
            elif item[:2] > recent[0][:2]:
                heapq.heapreplace(recent, item)
    history = []
    for _, _, row in sorted(recent):
        source, destination = int(row["src"]), int(row["dst"])
        history.append({
            "id": int(row["tx_id"]), "ts": int(row["ts"]), "src": source, "dst": destination,
            "direction": "outgoing" if source == account_id else "incoming",
            "counterparty": destination if source == account_id else source,
            "amount": float(row["amt_out"]), "currency": str(row["cur_out"]),
            "label": int(row["label"]), "matched_pattern": int(row["tx_id"]) in pattern_edge_ids,
        })
    return {
        "account": f"{account['bank']}:{account['account']}", "account_id": account_id,
        "total": total, "truncated": total > len(history), "history": history,
        "matched_pattern_edges": sum(bool(item["matched_pattern"]) for item in history),
    }


def _save_decision(settings: Settings, body: dict[str, Any]) -> dict[str, Any]:
    rank = body.get("rank")
    decision = body.get("decision")
    note = body.get("note", "")
    if isinstance(rank, bool) or not isinstance(rank, int) or rank < 1:
        raise ValueError("rank must be a positive integer")
    if not isinstance(decision, str) or decision not in {"accepted", "dismissed"}:
        raise ValueError("decision must be accepted or dismissed")
    if not isinstance(note, str) or len(note) > _MAX_NOTE_LENGTH:
        raise ValueError(f"note must be text no longer than {_MAX_NOTE_LENGTH} characters")
    if not any(int(alert.get("rank", 0)) == rank for alert in _saved_alerts(settings)):
        raise FileNotFoundError(f"saved alert rank {rank} was not found")
    path = _decisions_path(settings)
    with _DECISION_LOCK:
        decisions = _read_decisions(settings)
        result = {
            "decision": decision, "note": note.strip(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        decisions[str(rank)] = result
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(decisions, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)
    return {"rank": rank, **result}


def _compact_pattern_data(settings: Settings) -> list[dict[str, Any]]:
    files = {
        "fan": ("Fan in / fan out", ["fan"]),
        "scatter": ("Scatter & gather", ["scatter_gather"]),
        "cycle": ("Time-respecting cycles", ["cycle"]),
        "pass": ("Pass-through paths", ["pass_through"]),
    }
    result = []
    for key, (label, dirs) in files.items():
        reports = [_read_json(settings.output_dir / name / "report.json") for name in dirs]
        reports = [r for r in reports if isinstance(r, dict)]
        result.append({"id": key, "label": label,
            "hits": sum(int(report.get("hits", 0)) for report in reports),
            "ran": bool(reports), "fx_missing_hits": sum(int(report.get("fx_missing_hits", 0)) for report in reports)})
    return result


def api_data(endpoint: str, settings: Settings, query: dict[str, list[str]] | None = None) -> dict[str, Any] | None:
    """Build page data from saved AML artifacts."""
    if endpoint.startswith("investigation/"):
        try:
            return _investigation(endpoint, query or {}, settings)
        except (OSError, ValueError, KeyError, TypeError, ImportError) as exc:
            return {"error": f"investigation data unavailable: {exc}"}
    if endpoint in {"queue", "case", "case-report", "account-history"}:
        try:
            if endpoint == "queue":
                return _queue(settings, query or {})
            if endpoint == "case":
                return _case_from_query(settings, query or {}, _MAX_CASE_EDGES)
            if endpoint == "case-report":
                return _case_from_query(settings, query or {}, _MAX_REPORT_EDGES, 25)
            return _account_history(settings, query or {})
        except (OSError, ValueError, KeyError, TypeError, ImportError) as exc:
            return {"error": f"analyst data unavailable: {exc}"}
    checklist = _read_json(settings.output_dir / "m1" / "checklist.json")
    checklist = checklist if isinstance(checklist, dict) else {}
    final = _read_json(settings.output_dir / "m11" / "final_report.json")
    final = final if isinstance(final, dict) else None
    archived = _read_json(settings.output_dir / "m11" / "archived_exploratory" / "exploratory_report.json")
    archived = archived if isinstance(archived, dict) else None
    m8 = _read_json(settings.output_dir / "m8" / "report.json")
    m8 = m8 if isinstance(m8, dict) else {}
    split = checklist.get("split", {}).get("iso_boundaries", {})
    detectors = settings.detectors
    if endpoint == "overview":
        return {"transactions": checklist.get("transactions"), "positive_transactions": checklist.get("positive_transactions"),
            "accounts": checklist.get("accounts"), "detectors": len(_compact_pattern_data(settings)),
            "ranked_clusters": m8.get("clusters"), "test_report_available": final is not None}
    if endpoint == "approach":
        return {"train_end": split.get("train", [None, None])[1],
            "validation_end": split.get("validation", [None, None])[1],
            "fan_window_seconds": detectors.get("fan", {}).get("window_seconds"),
            "cycle_window_seconds": detectors.get("cycle", {}).get("window_seconds"),
            "pass_through_dwell_seconds": detectors.get("pass_through", {}).get("dwell_seconds"),
            "cutoff": (_read_json(settings.output_dir / "m9" / "frozen.yaml") or {}).get("cutoff")}
    if endpoint == "patterns":
        return {"patterns": _compact_pattern_data(settings)}
    if endpoint == "evaluation":
        report = final or archived
        account = report.get("account", {}) if report else {}
        recall_intervals = _read_json(settings.output_dir / "evaluation" / "attempt_recall_ci.json")
        if not (isinstance(recall_intervals, dict)
                and recall_intervals.get("source") == "outputs/m11/final_report.json"
                and recall_intervals.get("period") == "test"
                and recall_intervals.get("confidence_level") == 0.95):
            recall_intervals = {}
        ablation = _read_json(settings.output_dir / "evaluation" / "ablation.json")
        if not (isinstance(ablation, dict)
                and ablation.get("period") == "validation"
                and ablation.get("validation_only") is True
                and ablation.get("test_access") is False):
            ablation = {}
        posthoc = _read_json(settings.output_dir / "posthoc" / "baseline_test_comparison.json")
        if not (isinstance(posthoc, dict)
                and posthoc.get("period") == "test"
                and posthoc.get("post_hoc") is True
                and posthoc.get("frozen_system_config_changed") is False
                and posthoc.get("frozen_system_report_changed") is False):
            posthoc = None
        baseline = _read_json(settings.output_dir / "m3" / "baseline.json")
        baseline = baseline if isinstance(baseline, dict) else {}
        validation_baseline = baseline.get("metrics", {}).get("validation", {}).get("account", {})
        evasion: list[dict[str, Any]] = []
        evasion_report = _read_json(settings.output_dir / "evasion.json")
        evasion_is_validation = (isinstance(evasion_report, dict)
            and evasion_report.get("period") == "validation"
            and evasion_report.get("validation_only") is True)
        if evasion_is_validation:
            for row in evasion_report.get("rows", []):
                evasion.append({"kind": row["kind"], "strength": float(row["strength"]),
                    "main_recall": float(row["main_attempt_recall"]),
                    "baseline_recall": float(row["baseline_attempt_recall"])})
        if evasion_is_validation:
            scope = evasion_report.get("scope") or (
                "amount splitting, delays, and benign decoys; combined perturbation was not run")
            evasion_scope = f"Validation-only results from saved aml evasion output; {scope}."
        else:
            evasion_scope = None
        return {"available": report is not None, "report_status": "final" if final else ("exploratory" if archived else "missing"),
            "account_f1": account.get("f1"), "account_recall": account.get("recall"),
            "account_precision": account.get("precision"),
            "baseline_validation": {"precision": validation_baseline.get("precision"),
                "recall": validation_baseline.get("recall"), "f1": validation_baseline.get("f1"),
                "period": "validation", "note": "No baseline test metrics are saved; this is validation only."},
            "attempts": report.get("attempts_by_start_period") if report else None,
            "test_transactions": report.get("test_transactions") if report else None,
            "positive_test_transactions": report.get("positive_test_transactions") if report else None,
            "attempt_recall_by_typology": report.get("attempt_recall_edge_coverage_tau_0_5", {}) if report else {},
            "attempt_recall_intervals": recall_intervals.get("typologies", {}),
            "ablation": ablation.get("results", {}), "ablation_period": ablation.get("period"),
            "posthoc_baseline": posthoc,
            "evasion": evasion, "evasion_scope": evasion_scope,
            "synthetic_data": True}
    return None


def make_handler(settings: Settings, frontend_dir: Path = PROJECT_ROOT / "frontend") -> type[BaseHTTPRequestHandler]:
    class AMLHandler(BaseHTTPRequestHandler):
        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-store" if content_type == "application/json; charset=utf-8" else "no-cache")
            self.end_headers()
            self.wfile.write(body)

        def _send_json(self, status: int, value: dict[str, Any]) -> None:
            self._send(status, json.dumps(value, allow_nan=False).encode(), "application/json; charset=utf-8")

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            parsed = urlparse(self.path)
            route = parsed.path
            if route.startswith("/api/"):
                value = api_data(route.removeprefix("/api/"), settings, urllib.parse.parse_qs(parsed.query))
                if value is None:
                    self._send_json(404, {"error": "unknown endpoint"})
                else:
                    self._send_json(200, value)
                return
            name = "index.html" if route == "/" else route.lstrip("/")
            if name not in FRONTEND_FILES:
                self._send(404, b"Not found", "text/plain; charset=utf-8")
                return
            path = (PROJECT_ROOT / "README.md") if name == "README.md" else (frontend_dir / name)
            try:
                body = path.read_bytes()
            except OSError:
                self._send(404, b"Not found", "text/plain; charset=utf-8")
                return
            mime = "text/markdown; charset=utf-8" if name.endswith(".md") else (mimetypes.guess_type(name)[0] or "application/octet-stream") + "; charset=utf-8"
            self._send(200, body, mime)

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            if urlparse(self.path).path != "/api/case/decision":
                self._send_json(404, {"error": "unknown endpoint"})
                return
            if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                self._send_json(415, {"error": "Content-Type must be application/json"})
                return
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                self._send_json(400, {"error": "invalid Content-Length"})
                return
            if not 0 < length <= 16_384:
                self._send_json(413 if length > 16_384 else 400, {"error": "invalid request size"})
                return
            try:
                body = json.loads(self.rfile.read(length))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                self._send_json(400, {"error": f"invalid JSON body: {exc}"})
                return
            if not isinstance(body, dict):
                self._send_json(400, {"error": "request body must be a JSON object"})
                return
            try:
                result = _save_decision(settings, body)
            except FileNotFoundError as exc:
                self._send_json(404, {"error": str(exc)})
            except (ValueError, TypeError) as exc:
                self._send_json(400, {"error": str(exc)})
            except OSError as exc:
                self._send_json(500, {"error": f"could not save case decision: {exc}"})
            else:
                self._send_json(200, result)

        def log_message(self, format: str, *args: Any) -> None:
            print("frontend", self.address_string(), format % args)

    return AMLHandler


def serve(settings: Settings, host: str = "127.0.0.1", port: int = 8765) -> None:
    server = ThreadingHTTPServer((host, port), make_handler(settings))
    print(f"AML Graph frontend: http://{host}:{port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
