(() => {
  const rank = new URLSearchParams(window.location.search).get('rank');
  const byId = (id) => document.getElementById(id);
  let caseData = null;
  let historyRequest = 0;

  async function getJSON(url, options) {
    const response = await fetch(url, options);
    const data = await response.json();
    if (!response.ok || data.error) throw new Error(data.error || `Backend error ${response.status}`);
    return data;
  }

  function showError(message, target = 'case-status') {
    const node = byId(target);
    node.textContent = message;
    node.classList.add('error');
  }

  function renderConditions(conditions) {
    const list = byId('matched-conditions');
    list.replaceChildren();
    conditions.forEach((condition) => {
      const item = document.createElement('li');
      item.textContent = condition;
      list.append(item);
    });
    if (!conditions.length) list.textContent = 'No saved detector hit details.';
  }

  function renderTransfers(transfers, omitted) {
    const body = byId('case-transfers');
    body.replaceChildren();
    byId('transfer-count').textContent = `${transfers.length} shown${omitted ? ` · ${omitted} omitted` : ''}`;
    if (!transfers.length) {
      const row = document.createElement('tr');
      row.innerHTML = '<td colspan="5" class="empty-row">No saved transfer rows.</td>';
      body.append(row);
      return;
    }
    transfers.forEach((transfer) => {
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
      body.append(row);
    });
  }

  function renderDecision(decision) {
    byId('decision-state').textContent = decision.decision === 'pending'
      ? 'Not reviewed' : `${decision.decision[0].toUpperCase()}${decision.decision.slice(1)}${decision.updated_at ? ` · ${new Date(decision.updated_at).toLocaleString()}` : ''}`;
    byId('analyst-note').value = decision.note || '';
  }

  function renderAccounts(accounts) {
    const container = byId('case-accounts');
    container.replaceChildren();
    accounts.forEach((account) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.textContent = account;
      button.addEventListener('click', () => loadHistory(account, button));
      container.append(button);
    });
    if (accounts.length) container.querySelector('button').click();
  }

  async function loadHistory(account, selectedButton) {
    const request = ++historyRequest;
    document.querySelectorAll('#case-accounts button').forEach((button) => button.setAttribute('aria-pressed', 'false'));
    selectedButton.setAttribute('aria-pressed', 'true');
    byId('history-status').classList.remove('error');
    byId('history-status').textContent = `Loading transfers for ${account}…`;
    byId('account-history').replaceChildren();
    try {
      const data = await getJSON(`/api/account-history?account=${encodeURIComponent(account)}&rank=${encodeURIComponent(rank)}`);
      if (request !== historyRequest) return;
      byId('history-status').textContent = `${data.total.toLocaleString()} transfers total; showing ${data.history.length.toLocaleString()} most recent.`;
      data.history.forEach((transfer) => {
        const item = document.createElement('li');
        if (transfer.matched_pattern) item.classList.add('is-matched');
        const time = document.createElement('span');
        time.className = 'history-time';
        time.textContent = new Date(Number(transfer.ts) * 1000).toLocaleString(undefined, { timeZone: 'UTC' });
        const summary = document.createElement('span');
        summary.className = 'history-transfer';
        summary.textContent = `${transfer.direction} ${transfer.direction === 'outgoing' ? 'to' : 'from'} #${transfer.counterparty} · ${Number(transfer.amount).toLocaleString(undefined, { maximumFractionDigits: 2 })} ${transfer.currency}${transfer.matched_pattern ? ' · matched pattern' : ''}`;
        item.append(time, summary);
        byId('account-history').append(item);
      });
    } catch (error) {
      if (request === historyRequest) showError(error.message, 'history-status');
    }
  }

  async function saveDecision(decision, button) {
    button.disabled = true;
    try {
      const saved = await getJSON('/api/case/decision', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify({ rank: Number(rank), decision, note: byId('analyst-note').value }),
      });
      renderDecision(saved);
      byId('case-status').classList.remove('error');
      byId('case-status').textContent = `Saved ${saved.decision} decision locally.`;
    } catch (error) {
      showError(error.message);
    } finally {
      button.disabled = false;
    }
  }

  async function initialize() {
    if (!rank || !/^\d+$/.test(rank)) {
      showError('A valid case rank is required.');
      return;
    }
    byId('report-link').href = `case-report.html?rank=${encodeURIComponent(rank)}`;
    try {
      caseData = await getJSON(`/api/case?rank=${encodeURIComponent(rank)}`);
      byId('case-title').textContent = `Case #${caseData.rank} · ${caseData.typology.replaceAll('_', ' ')}`;
      byId('case-summary').textContent = `${caseData.account_count} accounts · ${caseData.pattern_edge_ids.length} matched transfers · ${caseData.time_range.start} to ${caseData.time_range.end}`;
      byId('case-score').textContent = `Score ${Number(caseData.score).toFixed(1)}%`;
      byId('case-narrative').textContent = caseData.summary;
      renderConditions(caseData.matched_conditions);
      renderTransfers(caseData.transfers, caseData.omitted_transfers);
      renderDecision(caseData.decision);
      renderAccounts(caseData.accounts);
      byId('case-status').textContent = `Loaded saved template alert ${caseData.alert_id}.`;
    } catch (error) {
      showError(error.message);
    }
  }

  byId('accept-case').addEventListener('click', (event) => saveDecision('accepted', event.currentTarget));
  byId('dismiss-case').addEventListener('click', (event) => saveDecision('dismissed', event.currentTarget));
  initialize();
})();
