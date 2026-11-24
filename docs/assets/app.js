/* NYC vice map */
(function () {
  'use strict';

  const CATS = ['beer', 'tobacco', 'liquor', 'cannabis']; // bottom to top
  const css = getComputedStyle(document.documentElement);
  const CAT_COLOR = {};
  CATS.forEach((c) => { CAT_COLOR[c] = css.getPropertyValue(`--${c}`).trim(); });
  const FG = css.getPropertyValue('--fg').trim() || '#f2f3ef';
  const CAT_LABEL = {
    liquor:   'Liquor or wine store',
    cannabis: 'Cannabis dispensary',
    tobacco:  'Tobacco / vape',
    beer:     'Beer (grocery, deli, drug store)',
  };
  const CAT_PLURAL = {
    liquor:   'liquor and wine stores',
    cannabis: 'cannabis dispensaries',
    tobacco:  'tobacco and vape sellers',
    beer:     'beer-licensed groceries and delis',
  };
  const SOURCE = {
    liquor:   ['NYS Liquor Authority', 'https://data.ny.gov/d/9s3h-dpkz'],
    beer:     ['NYS Liquor Authority', 'https://data.ny.gov/d/9s3h-dpkz'],
    cannabis: ['NYS Office of Cannabis Management', 'https://data.ny.gov/d/jskf-tt3q'],
    tobacco:  ['NYC Department of Consumer and Worker Protection', 'https://data.cityofnewyork.us/d/w7w3-xahh'],
  };
  const HOME = { center: [40.705, -73.95], zoom: 11 };

  const map = L.map('map', { preferCanvas: true, zoomSnap: 0.5 }).setView(HOME.center, HOME.zoom);

  // The page scrolls past the map to a table, so the wheel only zooms once the
  // reader has clicked into the map.
  map.scrollWheelZoom.disable();
  map.on('click focus', () => map.scrollWheelZoom.enable());
  map.getContainer().addEventListener('mouseleave', () => map.scrollWheelZoom.disable());
  L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png?key=cb1_2r82_1_ae4e70b6166057bc41b89638', {
    attribution: '&copy; OpenStreetMap &copy; CARTO',
    subdomains: 'abcd',
    maxZoom: 19,
  }).addTo(map);

  // One pane and canvas per category, so the rare layers always draw on top of
  // the common ones no matter which order they are switched on.
  const layers = {};
  const renderers = {};
  CATS.forEach((cat, i) => {
    map.createPane(`pane-${cat}`).style.zIndex = 400 + i;
    renderers[cat] = L.canvas({ pane: `pane-${cat}`, padding: 0.3 });
    layers[cat] = L.featureGroup().addTo(map);
  });

  let allFeatures = [];
  const DEFAULT_CATS = { liquor: true, cannabis: true, tobacco: true, beer: false };
  const state = {
    cats: { ...DEFAULT_CATS },
    borough: '',
    radiusMiles: 0.25,
    anchor: null,  // { lat, lon, label } once the reader picks an address
  };
  let table;
  let tableReady = false;

  function dotRadius(z) {
    if (z < 11.5) return 1.8;
    if (z < 12.5) return 2.4;
    if (z < 13.5) return 3.2;
    if (z < 14.5) return 4.2;
    if (z < 16) return 5.5;
    return 7;
  }

  function isHollow(p) {
    return p.category === 'cannabis' && !p.open;
  }

  function escapeHtml(s) {
    return String(s ?? '').replace(/[&<>"']/g, (c) => (
      { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
    ));
  }

  // AP style: Nov. 23, 2026; March, April, May, June and July are spelled out.
  function apDate(d) {
    const m = ['Jan.', 'Feb.', 'March', 'April', 'May', 'June', 'July', 'Aug.', 'Sept.', 'Oct.', 'Nov.', 'Dec.'][d.getMonth()];
    return `${m} ${d.getDate()}, ${d.getFullYear()}`;
  }

  function fmtDate(iso) {
    if (!iso) return '';
    return apDate(new Date(iso + 'T12:00:00'));
  }

  function statusLine(p) {
    if (p.category !== 'cannabis') return '';
    if (p.open) return p.opened ? `Open since ${fmtDate(p.opened)}` : 'Open to the public';
    return 'Licensed, not open to the public';
  }

  function popupHtml(p) {
    const [srcName, srcUrl] = SOURCE[p.category];
    const status = statusLine(p);
    const licenses = (p.license_count || 1) > 1
      ? `${p.license_count} licenses: ${escapeHtml(p.license_id)}`
      : `License ${escapeHtml(p.license_id || '—')}`;
    return `
      <div class="pop-name">${escapeHtml(p.name || '(unnamed)')}</div>
      <div class="pop-cat"><span class="pop-dot ${isHollow(p) ? 'hollow' : ''}" style="--c:${CAT_COLOR[p.category]}"></span>${escapeHtml(p.subcategory || CAT_LABEL[p.category])}</div>
      ${status ? `<div class="pop-status">${status}</div>` : ''}
      <div class="pop-addr">${escapeHtml(p.address || '')}</div>
      <div class="pop-meta">
        ${licenses}${p.expiration_date ? ` · expires ${fmtDate(p.expiration_date)}` : ''}<br>
        Source: <a href="${srcUrl}" target="_blank" rel="noopener">${escapeHtml(srcName)}</a>
      </div>
    `;
  }

  function makeMarker(feature) {
    const p = feature.properties;
    const [lon, lat] = feature.geometry.coordinates;
    const hollow = isHollow(p);
    const m = L.circleMarker([lat, lon], {
      renderer: renderers[p.category],
      radius: dotRadius(map.getZoom()) + (hollow ? 0.6 : 0),
      color: CAT_COLOR[p.category],
      fillColor: CAT_COLOR[p.category],
      fillOpacity: hollow ? 0 : 0.8,
      opacity: hollow ? 1 : 0.9,
      weight: hollow ? 1.4 : 0.6,
    });
    m.bindPopup(() => popupHtml(p), { maxWidth: 280 });
    return m;
  }

  function passesFilters(p) {
    if (!state.cats[p.category]) return false;
    if (state.borough && p.borough !== state.borough) return false;
    return true;
  }

  function applyFilters() {
    let visible = 0;
    CATS.forEach((cat) => layers[cat].clearLayers());
    for (const f of allFeatures) {
      if (!passesFilters(f.properties)) continue;
      layers[f.properties.category].addLayer(f._marker);
      visible++;
    }
    document.getElementById('visible-count').textContent = `${visible.toLocaleString()} on the map`;
    if (table && tableReady) table.setFilter(passesFilters);
    if (state.anchor) renderNearby(false);
  }

  map.on('zoomend', () => {
    const r = dotRadius(map.getZoom());
    for (const f of allFeatures) {
      f._marker.setRadius(r + (isHollow(f.properties) ? 0.6 : 0));
    }
  });

  function bindControls() {
    document.querySelectorAll('.chip.cat').forEach((el) => {
      el.addEventListener('click', () => {
        const on = el.getAttribute('aria-pressed') !== 'true';
        el.setAttribute('aria-pressed', String(on));
        state.cats[el.dataset.cat] = on;
        applyFilters();
      });
    });
    document.getElementById('borough-filter').addEventListener('change', (e) => {
      state.borough = e.target.value;
      applyFilters();
      if (state.borough && !state.anchor) {
        const pts = allFeatures.filter((f) => f.properties.borough === state.borough).map((f) => f._marker.getLatLng());
        if (pts.length) map.fitBounds(L.latLngBounds(pts), { padding: [20, 20] });
      }
    });
    setupAddressSearch();

    document.getElementById('radius-select').addEventListener('change', (e) => {
      state.radiusMiles = parseFloat(e.target.value);
      if (state.anchor) renderNearby(true);
    });

    document.getElementById('reset-btn').addEventListener('click', () => {
      state.cats = { ...DEFAULT_CATS };
      state.borough = '';
      document.querySelectorAll('.chip.cat').forEach((el) => el.setAttribute('aria-pressed', String(state.cats[el.dataset.cat])));
      document.getElementById('borough-filter').value = '';
      document.getElementById('search').value = '';
      clearAnchor();
      applyFilters();
      map.closePopup();
      map.setView(HOME.center, HOME.zoom);
    });
    document.getElementById('nearby-close').addEventListener('click', () => {
      document.getElementById('search').value = '';
      clearAnchor();
    });
  }

  // ---- Address search (GeoSearch autocomplete) + nearby panel ----
  let anchorMarker = null;
  let anchorCircle = null;

  function clearAnchor() {
    if (anchorMarker) { map.removeLayer(anchorMarker); anchorMarker = null; }
    if (anchorCircle) { map.removeLayer(anchorCircle); anchorCircle = null; }
    state.anchor = null;
    document.getElementById('nearby-panel').hidden = true;
  }

  function haversineMiles(a, b) {
    const R = 3958.7613;
    const toRad = (d) => (d * Math.PI) / 180;
    const dLat = toRad(b[0] - a[0]);
    const dLon = toRad(b[1] - a[1]);
    const h = Math.sin(dLat / 2) ** 2 + Math.sin(dLon / 2) ** 2 * Math.cos(toRad(a[0])) * Math.cos(toRad(b[0]));
    return 2 * R * Math.asin(Math.sqrt(h));
  }

  function setAnchor(lat, lon, label) {
    if (anchorMarker) map.removeLayer(anchorMarker);
    anchorMarker = L.marker([lat, lon], {
      icon: L.divIcon({ className: 'anchor-pin', iconSize: [16, 16], iconAnchor: [8, 8] }),
      keyboard: false,
      interactive: false,
      zIndexOffset: 1000,
    }).addTo(map);
    state.anchor = { lat, lon, label };
    renderNearby(true);
  }

  function renderNearby(refit) {
    if (!state.anchor) return;
    const { lat, lon, label } = state.anchor;
    const radius = state.radiusMiles;
    if (anchorCircle) map.removeLayer(anchorCircle);
    anchorCircle = L.circle([lat, lon], {
      radius: radius * 1609.34,
      color: FG,
      weight: 1.2,
      opacity: 0.8,
      dashArray: '3 4',
      fill: false,
      interactive: false,
    }).addTo(map);

    // The nearby count ignores the borough filter: a radius can cross a border.
    const within = [];
    const counts = { liquor: 0, cannabis: 0, tobacco: 0, beer: 0 };
    for (const f of allFeatures) {
      if (!state.cats[f.properties.category]) continue;
      const [flon, flat] = f.geometry.coordinates;
      const d = haversineMiles([lat, lon], [flat, flon]);
      if (d <= radius) {
        within.push({ f, d });
        counts[f.properties.category]++;
      }
    }
    within.sort((a, b) => a.d - b.d);

    document.getElementById('nearby-title').textContent = label;
    const radiusLabel = radius === 1 ? '1 mile' : `${radius} mile`;
    const shown = CATS.slice().reverse().filter((c) => state.cats[c]);
    document.getElementById('nearby-summary').innerHTML = [
      `<div class="ns-total">${within.length.toLocaleString()} within ${radiusLabel}</div>`,
      ...shown.map((c) =>
        `<span class="ns-cat"><span class="ns-dot" style="background:${CAT_COLOR[c]}"></span>${counts[c]} ${counts[c] === 1 ? CAT_LABEL[c].toLowerCase() : CAT_PLURAL[c]}</span>`
      ),
    ].join('');

    const list = document.getElementById('nearby-list');
    const LIMIT = 75;
    list.innerHTML = within.slice(0, LIMIT).map(({ f, d }, idx) => {
      const p = f.properties;
      const distLabel = d < 0.1 ? `${Math.round(d * 5280 / 10) * 10} ft` : `${d.toFixed(2)} mi`;
      const extra = isHollow(p) ? ' · not open' : '';
      return `
        <li data-idx="${idx}" tabindex="0">
          <span class="nl-dot ${isHollow(p) ? 'hollow' : ''}" style="--c:${CAT_COLOR[p.category]}"></span>
          <span class="nl-text">
            <span class="nl-name">${escapeHtml(p.name || '(unnamed)')}</span>
            <span class="nl-meta">${escapeHtml((p.address || '').split(',')[0])}${extra}</span>
          </span>
          <span class="nl-dist">${distLabel}</span>
        </li>
      `;
    }).join('') + (within.length > LIMIT ? `<li class="nl-more">Closest ${LIMIT} shown</li>` : '');

    const go = (li) => {
      const item = within[parseInt(li.dataset.idx, 10)];
      if (!item) return;
      const [flon, flat] = item.f.geometry.coordinates;
      map.setView([flat, flon], 18);
      if (!layers[item.f.properties.category].hasLayer(item.f._marker)) layers[item.f.properties.category].addLayer(item.f._marker);
      setTimeout(() => item.f._marker.openPopup(), 250);
    };
    list.onclick = (e) => { const li = e.target.closest('li[data-idx]'); if (li) go(li); };
    list.onkeydown = (e) => { if (e.key === 'Enter') { const li = e.target.closest('li[data-idx]'); if (li) go(li); } };

    document.getElementById('nearby-panel').hidden = false;
    if (refit) {
      const wide = map.getSize().x > 700;
      map.fitBounds(anchorCircle.getBounds(), wide
        ? { paddingTopLeft: [30, 30], paddingBottomRight: [340, 30] }
        : { paddingTopLeft: [20, 20], paddingBottomRight: [20, map.getSize().y * 0.5] });
    }
  }

  function setupAddressSearch() {
    const searchEl = document.getElementById('search');
    const sugEl = document.getElementById('suggestions');
    let activeIdx = -1;
    let currentSuggestions = [];
    let t;
    let lastFetchSeq = 0;

    function hide() {
      sugEl.hidden = true;
      searchEl.setAttribute('aria-expanded', 'false');
    }

    async function fetchAddressSuggestions(q) {
      const seq = ++lastFetchSeq;
      const url = 'https://geosearch.planninglabs.nyc/v2/autocomplete?size=8&text=' + encodeURIComponent(q);
      try {
        const r = await fetch(url);
        const data = await r.json();
        if (seq !== lastFetchSeq) return;
        const feats = (data.features || []).filter((f) => f.geometry && f.geometry.type === 'Point');
        currentSuggestions = feats;
        activeIdx = -1;
        if (!feats.length) {
          sugEl.innerHTML = '<div class="suggestion empty">No New York City address matches</div>';
          sugEl.hidden = false;
          return;
        }
        sugEl.innerHTML = feats.map((f, i) => {
          const p = f.properties || {};
          const sub = [p.borough, p.postalcode].filter(Boolean).join(' · ');
          return `
            <div class="suggestion" role="option" id="sug-${i}" data-idx="${i}">
              <span class="s-name">${escapeHtml(p.name || p.label || '')}</span>
              <span class="s-addr">${escapeHtml(sub)}</span>
            </div>
          `;
        }).join('');
        sugEl.hidden = false;
        searchEl.setAttribute('aria-expanded', 'true');
      } catch (e) {
        console.error('autocomplete error', e);
      }
    }

    function pickSuggestion(idx) {
      const f = currentSuggestions[idx];
      if (!f) return;
      const [lon, lat] = f.geometry.coordinates;
      const p = f.properties || {};
      const label = p.name ? `${p.name}${p.borough ? ', ' + p.borough : ''}` : (p.label || `${lat.toFixed(5)}, ${lon.toFixed(5)}`);
      searchEl.value = label;
      hide();
      setAnchor(lat, lon, label);
    }

    searchEl.addEventListener('input', (e) => {
      clearTimeout(t);
      const v = e.target.value.trim();
      if (v.length < 3) { hide(); return; }
      t = setTimeout(() => fetchAddressSuggestions(v), 180);
    });

    searchEl.addEventListener('keydown', (e) => {
      if (sugEl.hidden) return;
      const items = sugEl.querySelectorAll('.suggestion[data-idx]');
      const mark = () => {
        items.forEach((el, i) => el.classList.toggle('active', i === activeIdx));
        if (activeIdx >= 0) searchEl.setAttribute('aria-activedescendant', `sug-${activeIdx}`);
      };
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        activeIdx = Math.min(activeIdx + 1, items.length - 1);
        mark();
      } else if (e.key === 'ArrowUp') {
        e.preventDefault();
        activeIdx = Math.max(activeIdx - 1, 0);
        mark();
      } else if (e.key === 'Enter') {
        if (currentSuggestions.length) {
          e.preventDefault();
          pickSuggestion(activeIdx >= 0 ? activeIdx : 0);
        }
      } else if (e.key === 'Escape') {
        hide();
      }
    });

    sugEl.addEventListener('mousedown', (e) => {
      const item = e.target.closest('.suggestion[data-idx]');
      if (!item) return;
      e.preventDefault();
      pickSuggestion(parseInt(item.dataset.idx, 10));
    });

    searchEl.addEventListener('blur', () => setTimeout(hide, 150));
  }

  function buildTable() {
    const rows = allFeatures.map((f) => f.properties);
    table = new Tabulator('#table', {
      data: rows,
      layout: 'fitColumns',
      pagination: true,
      paginationSize: 25,
      paginationSizeSelector: [25, 50, 100, 250],
      placeholder: 'No locations match your filters.',
      initialSort: [{ column: 'name', dir: 'asc' }],
      columns: [
        {
          title: 'Category', field: 'category', width: 120, headerFilter: 'list',
          headerFilterParams: { values: { liquor: 'Liquor', cannabis: 'Cannabis', tobacco: 'Tobacco', beer: 'Beer' }, clearable: true },
          formatter: (cell) => {
            const p = cell.getRow().getData();
            return `<span class="cat-pill"><span class="pop-dot ${isHollow(p) ? 'hollow' : ''}" style="--c:${CAT_COLOR[p.category]}"></span>${p.category}</span>`;
          },
        },
        { title: 'Name', field: 'name', headerFilter: 'input', minWidth: 180 },
        { title: 'Address', field: 'address', headerFilter: 'input', minWidth: 220 },
        { title: 'Borough', field: 'borough', width: 120, headerFilter: 'list',
          headerFilterParams: { values: ['Manhattan', 'Brooklyn', 'Queens', 'Bronx', 'Staten Island'], clearable: true } },
        { title: 'License type', field: 'subcategory', width: 210, headerFilter: 'input',
          formatter: (cell) => {
            const p = cell.getRow().getData();
            const s = escapeHtml(cell.getValue() || '');
            return p.category === 'cannabis' && !p.open ? `${s} <span class="muted">· not open</span>` : s;
          } },
        { title: 'License #', field: 'license_id', width: 150 },
      ],
    });
    table.on('tableBuilt', () => {
      tableReady = true;
      table.setFilter(passesFilters);
    });
    table.on('rowClick', (e, row) => {
      const p = row.getData();
      const f = allFeatures.find((x) => x.properties.id === p.id);
      if (!f) return;
      const layer = layers[p.category];
      if (!layer.hasLayer(f._marker)) layer.addLayer(f._marker);
      document.getElementById('map').scrollIntoView({ behavior: 'smooth', block: 'center' });
      map.setView(f._marker.getLatLng(), 17);
      setTimeout(() => f._marker.openPopup(), 300);
    });
  }

  function writeSummary(meta) {
    // Counts come from the license rolls (methodology.json), mapped or not, so the
    // legend and the lede agree; the few records that could not be placed are
    // noted in the footer.
    const mapped = {};
    allFeatures.forEach((f) => { mapped[f.properties.category] = (mapped[f.properties.category] || 0) + 1; });
    const mappedOpen = allFeatures.filter((f) => f.properties.category === 'cannabis' && f.properties.open).length;
    const tot = (meta && meta.counts) || {};
    const pick = (k, fallback) => (tot[k] != null ? tot[k] : (fallback || 0));
    const c = {
      liquor: pick('liquor', mapped.liquor),
      tobacco: pick('tobacco', mapped.tobacco),
      beer: pick('beer', mapped.beer),
      cannabis: pick('cannabis', mapped.cannabis),
      cannabisOpen: pick('cannabis_open', mappedOpen),
    };
    const cannabisClosed = c.cannabis - c.cannabisOpen;
    const n = (x) => (x || 0).toLocaleString();

    const set = (k, txt) => { const el = document.querySelector(`[data-count="${k}"]`); if (el) el.textContent = txt; };
    set('liquor', n(c.liquor));
    set('tobacco', n(c.tobacco));
    set('beer', n(c.beer));
    set('cannabis', `${n(c.cannabisOpen)} open`);
    const ring = document.getElementById('ring-note');
    if (ring && cannabisClosed > 0) {
      ring.innerHTML = `<span class="pop-dot hollow" style="--c:${CAT_COLOR.cannabis}"></span>Rings are the ${n(cannabisClosed)} dispensaries licensed but not open to the public`;
    }

    // "more than 2 to 1", "about 12 to 1"
    const ratio = (a, b) => {
      if (!a || !b) return '';
      const r = a / b;
      const f = Math.floor(r);
      return r - f >= 0.1 ? `more than ${f} to 1` : `${f} to 1`;
    };
    if (c.tobacco && c.liquor && c.cannabisOpen) {
      document.getElementById('finding').textContent =
        `Licensed tobacco and vape sellers outnumber the city's liquor stores ${ratio(c.tobacco, c.liquor)} ` +
        `and its open legal cannabis dispensaries ${ratio(c.tobacco, c.cannabisOpen)}.`;
    }
    document.getElementById('lede').innerHTML =
      `The map shows <b>${n(c.tobacco)}</b> tobacco and vape sellers, <b>${n(c.liquor)}</b> liquor and wine stores ` +
      `and <b>${n(c.cannabisOpen)}</b> legal cannabis dispensaries open to the public, plus ${n(cannabisClosed)} more that hold a license but have not opened. ` +
      `Another <b>${n(c.beer)}</b> groceries, delis and drug stores are licensed to sell beer; switch them on below. ` +
      `Search an address to count what is within walking distance.`;

    if (meta) {
      const d = new Date(meta.generated_at);
      document.getElementById('asof').textContent =
        `Licenses as of ${apDate(d)}`;
      const notMapped = (meta.counts && meta.counts.total ? meta.counts.total - meta.mapped_features : 0);
      document.getElementById('meta-line').textContent =
        `Last refreshed ${apDate(d)}. ` +
        `${meta.mapped_features.toLocaleString()} licensed locations mapped` +
        (notMapped > 0 ? `; ${notMapped} more could not be placed reliably and are listed in the CSV.` : '.');
    }
  }

  Promise.all([
    fetch('data/locations.geojson').then((r) => { if (!r.ok) throw new Error(r.status); return r.json(); }),
    fetch('data/methodology.json').then((r) => r.json()).catch(() => null),
  ]).then(([geo, meta]) => {
    allFeatures = geo.features;
    allFeatures.forEach((f) => { f._marker = makeMarker(f); });
    writeSummary(meta);
    buildTable();
    bindControls();
    applyFilters();
  }).catch((err) => {
    console.error(err);
    document.getElementById('meta-line').textContent = 'Could not load data.';
    document.getElementById('lede').textContent = 'The map data did not load. Try reloading the page.';
  });
})();
