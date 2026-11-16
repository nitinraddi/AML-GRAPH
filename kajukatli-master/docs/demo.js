(() => {
  const detectorSelect = document.getElementById('detector-select');
  const caseSelect = document.getElementById('case-select');
  const status = document.getElementById('demo-status');
  let bundle;

  function setStatus(message, error = false) {
    status.textContent = message;
    status.classList.toggle('error', error);
  }

  function renderCase() {
    const detector = bundle.detectors.find((row) => row.id === detectorSelect.value);
    const selected = detector?.cases.find((row) => row.id === caseSelect.value);
    if (!selected) return;
    document.getElementById('case-card').hidden = false;
    document.getElementById('case-detector').textContent = detector.label;
    document.getElementById('case-title').textContent = selected.typology.replaceAll('_', ' ');
    document.getElementById('transfer-count').textContent = `${selected.transfers.length} transfers`;
    document.getElementById('case-time').textContent = `${selected.start} to ${selected.end}`;

    const accounts = document.getElementById('account-list');
    accounts.replaceChildren();
    selected.accounts.forEach((account) => {
      const item = document.createElement('li');
      item.textContent = account;
      accounts.append(item);
    });

    const rows = document.getElementById('transfer-rows');
    rows.replaceChildren();
    selected.transfers.forEach((transfer) => {
      const row = document.createElement('tr');
      [
        new Date(transfer.time).toLocaleString(undefined, { timeZone: 'UTC' }),
        transfer.from,
        transfer.to,
        Number(transfer.amount).toLocaleString(undefined, { maximumFractionDigits: 2 }),
        transfer.currency,
      ].forEach((value) => {
        const cell = document.createElement('td');
        cell.textContent = String(value);
        row.append(cell);
      });
      rows.append(row);
    });
  }

  function renderDetector() {
    const detector = bundle.detectors.find((row) => row.id === detectorSelect.value);
    caseSelect.replaceChildren();
    detector.cases.forEach((sample, index) => {
      const option = new Option(`${index + 1}. ${sample.typology.replaceAll('_', ' ')}`, sample.id);
      caseSelect.add(option);
    });
    caseSelect.disabled = detector.cases.length < 1;
    renderCase();
  }

  async function loadBundle() {
    try {
      const response = await fetch('./demo-data.json', { headers: { Accept: 'application/json' } });
      if (!response.ok) throw new Error(`Could not load static demo data (${response.status}).`);
      bundle = await response.json();
      if (bundle.read_only !== true || bundle.banner !== 'Demo data excerpt - synthetic'
          || !Array.isArray(bundle.detectors) || bundle.detectors.length !== 4) {
        throw new Error('The static demo data has an unexpected format.');
      }
      document.getElementById('demo-banner').textContent = bundle.banner;
      detectorSelect.replaceChildren();
      bundle.detectors.forEach((detector) => detectorSelect.add(
        new Option(detector.label, detector.id)));
      detectorSelect.disabled = false;
      renderDetector();
      setStatus(`Loaded ${bundle.detectors.reduce((count, row) => count + row.cases.length, 0)} saved sample cases.`);
    } catch (error) {
      setStatus(error.message, true);
    }
  }

  detectorSelect.addEventListener('change', renderDetector);
  caseSelect.addEventListener('change', renderCase);
  loadBundle();
})();
