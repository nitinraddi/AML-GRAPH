(() => {
  const rank = new URLSearchParams(window.location.search).get('rank');
  const byId = (id) => document.getElementById(id);

  async function loadReport() {
    if (!rank || !/^\d+$/.test(rank)) throw new Error('A valid case rank is required.');
    const response = await fetch(`/api/case-report?rank=${encodeURIComponent(rank)}`, {
      headers: { Accept: 'application/json' },
    });
    const data = await response.json();
    if (!response.ok || data.error) throw new Error(data.error || `Backend error ${response.status}`);
    byId('report-title').textContent = `Case report · ${data.alert_id}`;
    byId('report-summary').textContent = data.summary;
    byId('report-typology').textContent = data.typology.replaceAll('_', ' ');
    byId('report-score').textContent = `${Number(data.score).toFixed(1)}%`;
    byId('report-time').textContent = `${data.time_range.start} to ${data.time_range.end}`;
    byId('report-decision').textContent = data.decision.decision === 'pending'
      ? 'Not reviewed' : `${data.decision.decision} · ${data.decision.updated_at || ''}`;
    const accountList = data.accounts.slice(0, 25);
    byId('report-accounts').textContent = `${accountList.join(', ')}${data.omitted_accounts ? `, and ${data.omitted_accounts} more` : ''}`;
    const conditions = byId('report-conditions');
    conditions.replaceChildren();
    data.matched_conditions.forEach((condition) => {
      const item = document.createElement('li');
      item.textContent = condition;
      conditions.append(item);
    });
    const transfers = byId('report-transfers');
    transfers.replaceChildren();
    data.transfers.forEach((transfer) => {
      const row = document.createElement('tr');
      [
        new Date(transfer.ts).toLocaleString(undefined, { timeZone: 'UTC' }),
        transfer.src, transfer.dst,
        Number(transfer.amount).toLocaleString(undefined, { maximumFractionDigits: 2 }),
        transfer.currency,
      ].forEach((value) => {
        const cell = document.createElement('td');
        cell.textContent = String(value);
        row.append(cell);
      });
      transfers.append(row);
    });
    byId('report-omitted').textContent = data.omitted_transfers
      ? `${data.omitted_transfers} additional transfers omitted from this one-page summary.` : '';
    byId('report-note').textContent = data.decision.note || 'No analyst note recorded.';
    byId('report-status').textContent = 'Report generated from saved case evidence and template narrative.';
  }

  byId('print-report').addEventListener('click', () => window.print());
  loadReport().catch((error) => {
    byId('report-status').textContent = error.message;
    byId('report-status').classList.add('error');
  });
})();
