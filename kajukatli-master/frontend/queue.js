(() => {
  const rows = document.getElementById('queue-rows');
  const typeFilter = document.getElementById('typology-filter');
  const scoreFilter = document.getElementById('score-filter');
  let savedAlerts = [];

  async function getJSON(url) {
    const response = await fetch(url, { headers: { Accept: 'application/json' } });
    const data = await response.json();
    if (!response.ok || data.error) throw new Error(data.error || `Backend error ${response.status}`);
    return data;
  }

  function setStatus(message, error = false) {
    const node = document.getElementById('queue-status');
    node.textContent = message;
    node.classList.toggle('error', error);
  }

  function addCell(row, value, className = '') {
    const cell = document.createElement('td');
    cell.textContent = String(value);
    if (className) cell.className = className;
    row.append(cell);
  }

  function render() {
    const typology = typeFilter.value;
    const minimum = Number(scoreFilter.value || 0);
    const visible = savedAlerts.filter((alert) =>
      (!typology || alert.typologies.includes(typology)) && alert.score >= minimum);
    rows.replaceChildren();
    visible.forEach((alert) => {
      const row = document.createElement('tr');
      addCell(row, `#${alert.rank}`, 'queue-rank');
      addCell(row, `${Number(alert.score).toFixed(1)}%`, 'queue-score');
      addCell(row, alert.typologies.map((value) => value.replaceAll('_', ' ')).join(', '), 'queue-type');
      addCell(row, alert.account_count);
      addCell(row, alert.transfer_count);
      const status = document.createElement('td');
      const chip = document.createElement('span');
      chip.className = `decision-chip ${alert.decision}`;
      chip.textContent = alert.decision === 'pending' ? 'Not reviewed' : alert.decision[0].toUpperCase() + alert.decision.slice(1);
      if (alert.note) chip.title = alert.note;
      status.append(chip);
      row.append(status);
      const action = document.createElement('td');
      const link = document.createElement('a');
      link.className = 'queue-case-link';
      link.href = `case.html?rank=${encodeURIComponent(alert.rank)}`;
      link.textContent = 'Review';
      action.append(link);
      row.append(action);
      rows.append(row);
    });
    if (!visible.length) {
      const row = document.createElement('tr');
      const cell = document.createElement('td');
      cell.className = 'empty-row';
      cell.colSpan = 7;
      cell.textContent = 'No saved alerts match these filters.';
      row.append(cell);
      rows.append(row);
    }
    document.getElementById('filter-count').textContent = `Showing ${visible.length} of ${savedAlerts.length}`;
  }

  async function loadQueue() {
    setStatus('Loading saved ranked alerts…');
    try {
      const data = await getJSON('/api/queue?limit=100');
      savedAlerts = data.alerts;
      document.getElementById('queue-count').textContent = `${data.exported_alerts}`;
      const metric = data.alerts_per_true_hit;
      document.getElementById('alerts-per-hit').textContent = metric?.available
        ? Number(metric.alerts_per_true_hit).toFixed(1) : 'Not available';
      document.getElementById('hit-summary').textContent = metric?.available
        ? `From the saved ${metric.period} report: ${metric.alerts} alerts / ${metric.true_hits} true hits at the ${Math.round(metric.positive_edge_fraction_threshold * 100)}% positive-edge threshold.`
        : 'No saved cluster precision report is available yet.';
      const existing = new Set([...typeFilter.options].map((option) => option.value));
      data.typologies.forEach((typology) => {
        if (existing.has(typology)) return;
        const option = new Option(typology.replaceAll('_', ' '), typology);
        typeFilter.add(option);
      });
      setStatus(`Loaded ${data.exported_alerts} saved ranked alert candidates.`);
      render();
    } catch (error) {
      savedAlerts = [];
      rows.replaceChildren();
      const row = document.createElement('tr');
      const cell = document.createElement('td');
      cell.colSpan = 7;
      cell.className = 'empty-row';
      cell.textContent = error.message;
      row.append(cell);
      rows.append(row);
      setStatus(error.message, true);
    }
  }

  typeFilter.addEventListener('change', render);
  scoreFilter.addEventListener('input', render);
  document.getElementById('clear-filters').addEventListener('click', () => {
    typeFilter.value = '';
    scoreFilter.value = '0';
    render();
  });
  window.addEventListener('pageshow', loadQueue);
  loadQueue();
})();
