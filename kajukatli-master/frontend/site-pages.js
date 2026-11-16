(() => {
  const nav = document.querySelector('.site-nav');
  const button = document.querySelector('.menu-toggle');
  const links = document.querySelector('.nav-links');
  if (nav) window.addEventListener('scroll', () => nav.classList.toggle('is-scrolled', window.scrollY > 24), { passive: true });
  if (button && links) {
    button.addEventListener('click', () => {
      const open = button.getAttribute('aria-expanded') !== 'true';
      button.setAttribute('aria-expanded', String(open));
      button.setAttribute('aria-label', open ? 'Close navigation' : 'Open navigation');
      links.classList.toggle('is-open', open);
    });
    links.addEventListener('click', event => {
      if (event.target.closest('a')) {
        links.classList.remove('is-open');
        button.setAttribute('aria-expanded', 'false');
        button.setAttribute('aria-label', 'Open navigation');
      }
    });
  }
  const targets = document.querySelectorAll('.reveal');
  if ('IntersectionObserver' in window && !window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
    const observer = new IntersectionObserver(entries => entries.forEach(entry => {
      if (entry.isIntersecting) { entry.target.classList.add('is-visible'); observer.unobserve(entry.target); }
    }), { threshold: .15 });
    targets.forEach(target => observer.observe(target));
  } else targets.forEach(target => target.classList.add('is-visible'));

  const page = document.body.dataset.page;
  if (page && page !== 'investigation') {
    fetch(`/api/${page}`, { headers: { Accept: 'application/json' } })
      .then(response => { if (!response.ok) throw new Error('Backend data is not available'); return response.json(); })
      .then(data => renderPageData(page, data))
      .catch(() => document.querySelectorAll('[data-backend-status]').forEach(node => {
        node.textContent = 'Backend not connected · run aml web from the project root';
        node.classList.add('is-offline');
      }));
  }

  function renderPageData(name, data) {
    document.querySelectorAll('[data-backend-status]').forEach(node => {
      node.textContent = name === 'evaluation'
        ? (data.report_status === 'final' ? 'Backend connected · final report loaded'
          : data.report_status === 'exploratory' ? 'Backend connected · archived exploratory report loaded (not final)'
            : 'Backend connected · no saved evaluation report')
        : 'Backend connected · live project outputs';
    });
    if (name === 'overview') {
      set('[data-value="transactions"]', format(data.transactions));
      set('[data-value="accounts"]', format(data.accounts));
      set('[data-value="ranked-clusters"]', format(data.ranked_clusters));
      set('[data-value="test-state"]', data.test_report_available ? 'Final report available' : 'Awaiting final report');
    }
    if (name === 'approach') {
      set('[data-value="fan-window"]', duration(data.fan_window_seconds));
      set('[data-value="cycle-window"]', duration(data.cycle_window_seconds));
      set('[data-value="pass-window"]', duration(data.pass_through_dwell_seconds));
      set('[data-value="cutoff"]', data.cutoff == null ? 'Not frozen' : Number(data.cutoff).toFixed(3));
      set('[data-value="train-end"]', data.train_end || 'Not prepared');
      set('[data-value="validation-end"]', data.validation_end || 'Not prepared');
    }
    if (name === 'patterns') {
      (data.patterns || []).forEach(pattern => {
        const row = document.querySelector(`[data-pattern-id="${pattern.id}"] .pattern-live`);
        if (row) row.textContent = pattern.ran ? `${format(pattern.hits)} candidate hits in latest detector run` : 'No detector output yet';
      });
    }
    if (name === 'evaluation') {
      set('[data-value="account-f1"]', percent(data.account_f1));
      set('[data-value="account-precision"]', percent(data.account_precision));
      set('[data-value="account-recall"]', percent(data.account_recall));
      set('[data-value="baseline-f1"]', percent(data.baseline_validation?.f1));
      set('[data-value="baseline-precision"]', percent(data.baseline_validation?.precision));
      set('[data-value="baseline-recall"]', percent(data.baseline_validation?.recall));
      set('[data-value="attempt-count"]', format(data.attempts));
      set('[data-value="test-edges"]', format(data.test_transactions));
      set('[data-value="positive-edges"]', format(data.positive_test_transactions));
      set('[data-value="test-edges-note"]', format(data.test_transactions));
      set('[data-value="positive-edges-note"]', format(data.positive_test_transactions));

      const typologies = document.querySelector('[data-typology-results]');
      if (typologies) {
        typologies.replaceChildren();
        const results = Object.entries(data.attempt_recall_by_typology || {});
        if (!results.length) {
          const row = document.createElement('li');
          row.textContent = data.report_status === 'exploratory'
            ? 'Typology recall is not included in the archived exploratory report.'
            : 'No saved typology results.';
          typologies.append(row);
        }
        results.forEach(([name, result]) => {
          const row = document.createElement('li');
          const metric = result.thresholds?.find(item => Number(item.tau) === .5);
          const detected = metric ? metric.edge_detected : result.detected;
          const recall = metric ? metric.edge_recall : result.recall;
          const interval = data.attempt_recall_intervals?.[name]?.ci95;
          const label = document.createElement('span');
          const value = document.createElement('span');
          label.textContent = name;
          value.textContent = result.status === 'not covered' ? 'Not covered'
            : `${format(detected)} / ${format(result.attempts)} - ${percent(recall)}${interval ? ` (95% CI ${percent(interval[0])} to ${percent(interval[1])})` : ''}`;
          row.append(label, value);
          typologies.append(row);
        });
      }

      const ablation = document.querySelector('[data-ablation-results]');
      if (ablation) {
        ablation.replaceChildren();
        const labels = {
          rule_detectors_only: 'Rule detectors only',
          transparent_score_only: 'Transparent score only',
          full_xgboost_cluster_score: 'Full XGBoost cluster score',
        };
        Object.entries(data.ablation || {}).forEach(([name, metrics]) => {
          const row = document.createElement('tr');
          [labels[name] || name, percent(metrics.f1), percent(metrics.precision), percent(metrics.recall), percent(metrics.pr_auc), cutoff(metrics.cutoff)]
            .forEach(value => { const cell = document.createElement('td'); cell.textContent = value; row.append(cell); });
          ablation.append(row);
        });
        if (!ablation.children.length) {
          const row = document.createElement('tr');
          row.innerHTML = '<td colspan="6">No saved validation ablation. Run <code>aml evaluation-artifacts</code>.</td>';
          ablation.append(row);
        }
      }

      const posthocBody = document.querySelector('[data-posthoc-results]');
      const posthocStatus = document.querySelector('[data-posthoc-status]');
      if (posthocBody) {
        posthocBody.replaceChildren();
        const posthoc = data.posthoc_baseline;
        const system = posthoc?.system_reference_from_saved_final_report;
        const baseline = posthoc?.baseline?.account;
        if (system && baseline) {
          posthocStatus.textContent = `${posthoc.label} - ${format(posthoc.test_only_input_rows)} test transactions`;
          [['F1', 'f1'], ['Precision', 'precision'], ['Recall', 'recall'], ['PR-AUC', 'pr_auc']]
            .forEach(([label, key]) => {
              const row = document.createElement('tr');
              [label, percent(system[key]), percent(baseline[key])].forEach(value => {
                const cell = document.createElement('td'); cell.textContent = value; row.append(cell);
              });
              posthocBody.append(row);
            });
        } else {
          posthocStatus.textContent = 'No saved post-hoc test comparison. Run aml evaluation-artifacts to generate it.';
          const row = document.createElement('tr');
          row.innerHTML = '<td colspan="3">No saved post-hoc baseline comparison.</td>';
          posthocBody.append(row);
        }
      }

      const curve = document.querySelector('[data-evasion-results]');
      if (curve) {
        curve.replaceChildren();
        const evasionStatus = document.querySelector('[data-evasion-status]');
        if (evasionStatus) evasionStatus.textContent = data.evasion_scope || 'No saved validation stress-test results. Run aml evaluation-artifacts to generate them.';
        (data.evasion || []).forEach(point => {
          const row = document.createElement('li');
          const label = document.createElement('span');
          const value = document.createElement('span');
          label.textContent = `${point.kind.split('_').join(' ')} ${percent(point.strength)}`;
          value.textContent = `Graph recall ${percent(point.main_recall)} | baseline recall ${percent(point.baseline_recall)}`;
          row.append(label, value);
          curve.append(row);
        });
        if (!curve.children.length) {
          const row = document.createElement('li');
          row.textContent = 'No saved stress-test rows.';
          curve.append(row);
        }
      }
    }
  }

  function set(selector, value) { const node = document.querySelector(selector); if (node) node.textContent = value; }
  function format(value) { return value == null ? '—' : Number(value).toLocaleString(); }
  function percent(value) { return value == null || !Number.isFinite(Number(value)) ? '—' : `${(Number(value) * 100).toFixed(1)}%`; }
  function cutoff(value) { return value == null || !Number.isFinite(Number(value)) ? '—' : Number(value).toPrecision(4); }
  function duration(value) { return value == null ? '—' : `${(Number(value) / 3600).toFixed(1)} hours`; }
})();
