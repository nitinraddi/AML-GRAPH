# AML Graph: analyst workflow and evaluation

## Who it is for

AML Graph is a research prototype for bank compliance analysts and financial-crime investigators who need to triage connected-transfer candidates and understand why a graph detector surfaced them. It is not a production AML platform and does not determine that a customer committed a crime.

The workflow is:

1. Review ranked candidate clusters and filter by score or typology.
2. Open a case to inspect its deterministic narrative, matched conditions, flagged transfers, and an account's surrounding transfer history.
3. Record Accept or Dismiss with an analyst note.
4. Export a printable one-page summary from the saved evidence.

The local website stores decisions in a JSON file. The hosted demo is deliberately static and read-only; it shows only a small sample of synthetic cases.

## Results worth discussing

The numbers below are computed from the checked-in run's saved `outputs/m11/final_report.json`; the separate baseline values come from `outputs/posthoc/baseline_test_comparison.json`. They are synthetic HI-Small benchmark results, not estimates of performance at a bank.

### Alerts per true cluster hit

At the report's 50% positive-edge-fraction criterion, the frozen system reports **2,612 alert clusters** with **10.4518% cluster precision**.

```text
Estimated true cluster hits = round(alert_count × cluster_precision)
                            = round(2,612 × 0.1045176)
                            = 273
Alerts per estimated true hit = alert_count ÷ estimated true cluster hits
                              = 2,612 ÷ 273
                              = 9.57
```

This is an estimate based on rounded report precision, not an exact operational workload rate. It means roughly 9.6 benchmark alert clusters per cluster counted as a true hit under this report's criterion.

### Account-level precision over base rate

The saved test report has 356,396 accounts in its evaluated population: TP=546, FP=4,463, FN=1,655, TN=349,732.

```text
Account precision = TP ÷ (TP + FP)
                  = 546 ÷ (546 + 4,463)
                  = 10.90%
Positive account base rate = (TP + FN) ÷ (TP + FP + FN + TN)
                           = (546 + 1,655) ÷ 356,396
                           = 0.6176%
Precision-over-base-rate = account precision ÷ positive account base rate
                         = 10.90% ÷ 0.6176%
                         = 17.65×
```

This lift compares **account-level** precision with the positive-account prevalence in the same test population. It is not cluster precision, does not account for investigation costs, and should not be read as a real-world uplift.

## Separate baseline comparison and caveats

The saved post-hoc comparison applies the already train-tuned threshold baseline to test-period transactions only. It was generated separately after the frozen graph-system report and does not modify that report or the frozen system configuration.

| Test account metric | Frozen graph system | Post-hoc threshold baseline |
|---|---:|---:|
| Precision | 10.90% | 1.28% |
| Recall | 24.81% | 24.94% |
| F1 | 15.15% | 2.44% |
| PR-AUC | 15.16% | 1.02% |

This is a post-hoc benchmark comparison, not a prospective or production trial. Both account metrics use the saved test report population, but the methods produce different alert sets. At a 50% positive-edge criterion, the system reports 2,612 clusters at 10.45% precision, while the baseline reports 28,039 at 0.83% precision; differing candidate generation and alert volume mean this is not an equal-workload comparison. The baseline recall is slightly higher, so the result should not be simplified to “the system is better” without acknowledging the different outputs and operating points.

## Limitations and roadmap

The benchmark is synthetic and cannot establish performance on real bank data. Detector coverage and recall vary substantially by typology. On the final test report at 50% edge coverage:

| Typology | Attempt edge recall |
|---|---:|
| Fan-out | 0 / 10 (0%) |
| Gather-scatter | 0 / 16 (0%) |
| Stack | 0 / 7 (0%) |

Planned follow-up is to:

- **Fan-out:** investigate candidate generation and temporal/value constraints, add focused fixtures, and evaluate changes using validation data before any new frozen test run.
- **Gather-scatter:** improve multi-stage linkage and evidence assembly, then measure validation coverage separately from scatter-gather.
- **Stack:** design and implement a dedicated sequential-stacking detector; current zero recall is a report finding, not evidence that stack detection is implemented.
- Evaluate analyst workload, calibration, robustness, data drift, false-positive cost, and privacy/security requirements with appropriately governed real-world validation before considering deployment.

The current interface is a research workflow, not an integrated alerting system. It has no institution identity resolution, policy controls, audit platform, or external data feeds.
