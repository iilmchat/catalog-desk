'use strict';

const $ = (s, root = document) => root.querySelector(s);
const $$ = (s, root = document) => Array.from(root.querySelectorAll(s));
const state = { view: 'dashboard', page: 1, productPages: 1, productTotal: 0, files: [], polling: false, toastTimer: null };

function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
}
function money(value) {
  if (value === null || value === undefined || value === '') return '—';
  return new Intl.NumberFormat('ru-RU', {minimumFractionDigits: 2, maximumFractionDigits: 2}).format(Number(value)) + ' ₽';
}
function num(value) { return new Intl.NumberFormat('ru-RU').format(Number(value || 0)); }
function date(value, withTime = true) {
  if (!value) return '—';
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return String(value).replace('T',' ').slice(0,19);
  return d.toLocaleString('ru-RU', withTime ? {day:'2-digit',month:'2-digit',year:'numeric',hour:'2-digit',minute:'2-digit'} : {day:'2-digit',month:'2-digit',year:'numeric'});
}
function safeUrl(value) {
  try { const u = new URL(value, location.origin); return ['http:','https:'].includes(u.protocol) ? u.href : ''; }
  catch (_) { return ''; }
}
function toast(message) {
  const el = $('#toast'); el.textContent = message; el.classList.add('show');
  clearTimeout(state.toastTimer); state.toastTimer = setTimeout(() => el.classList.remove('show'), 2800);
}
async function api(path, options = {}) {
  const res = await fetch(path, options);
  const type = res.headers.get('content-type') || '';
  const data = type.includes('application/json') ? await res.json() : await res.text();
  if (!res.ok) throw new Error(data?.error || data || `HTTP ${res.status}`);
  return data;
}
function setView(view) {
  state.view = view;
  $$('.view').forEach(el => el.classList.toggle('active', el.id === `view-${view}`));
  $$('.nav-link').forEach(el => el.classList.toggle('active', el.dataset.view === view));
  const heading = {dashboard:'Обзор', products:'Товары', changes:'Изменения цен', collection:'Сбор данных'}[view] || 'Обзор';
  $('#page-heading').textContent = heading;
  $('#sidebar').classList.remove('open');
  if (view === 'dashboard') loadDashboard();
  if (view === 'products') loadProducts();
  if (view === 'changes') loadChanges();
  if (view === 'collection') loadCollection();
}

async function loadDashboard() {
  try {
    const d = await api('/api/dashboard');
    $('#nav-product-count').textContent = num(d.products);
    $('#today-chip').textContent = new Date().toLocaleDateString('ru-RU', {day:'numeric',month:'long',year:'numeric'});
    const cards = [
      [num(d.products), 'Товаров в базе', 'Уникальные SAP + PLU'],
      [num(d.with_discount), 'Сейчас со скидкой', 'По последнему наблюдению'],
      [`${num(d.with_local_image)} / ${num(d.with_image_url)}`, 'Фотографии', 'Локально / URL'],
      [num(d.observations), 'Снимки цен', 'Исторические наблюдения'],
    ];
    $('#stat-grid').innerHTML = cards.map((c,i) => `<div class="stat-card"><div class="stat-label">${c[1]}</div><div class="stat-value">${c[0]}</div><div class="stat-foot">${c[2]}</div></div>`).join('');
    const cats = (d.category_progress || []).filter(c => c.files > 0).sort((a,b) => b.unique_products-a.unique_products).slice(0,8);
    const max = Math.max(1, ...cats.map(c => c.unique_products));
    $('#category-bars').innerHTML = cats.length ? cats.map(c => `<div class="category-row"><div class="category-name" title="${esc(c.category)}">${esc(c.category)}</div><div class="bar-track"><div class="bar-fill" style="width:${Math.max(1,c.unique_products/max*100)}%"></div></div><div class="category-num">${num(c.unique_products)}</div></div>`).join('') : '<div class="empty-inline">Пока нет импортированных категорий.</div>';
    const changes = d.recent_changes || [];
    $('#recent-changes').innerHTML = changes.length ? changes.slice(0,6).map(renderRecentChange).join('') : '<div class="empty-inline">Изменений пока нет. Повторный снимок той же страницы создаст точку сравнения.</div>';
    const exports = d.recent_exports || [];
    $('#recent-exports').innerHTML = exports.length ? exports.slice(0,5).map(e => `<div class="export-item"><div class="export-ico">⇧</div><div class="export-main"><strong>${esc(e.category || e.file_name)}</strong><small>${esc(e.file_name)} · стр. ${esc(e.page_number || '—')}</small></div><div class="recent-delta">${num(e.product_rows)}<small style="display:block;color:#9aa49c;font-weight:400">${date(e.imported_at,false)}</small></div></div>`).join('') : '<div class="empty-inline">Импортов пока нет.</div>';
  } catch (err) { showDbError(err); }
}
function renderRecentChange(c) {
  const up = Number(c.delta || 0) > 0;
  const text = c.delta === null ? 'условия' : `${up ? '+' : ''}${money(c.delta)}`;
  return `<div class="recent-item"><div class="recent-marker ${up?'up':'down'}">${up?'↑':'↓'}</div><div class="recent-main"><strong>${esc(c.name || 'Товар')} · ${esc(c.plu)}</strong><small>${esc(c.category || '')} · ${date(c.captured_at,false)}</small></div><div class="recent-delta ${up?'up':'down'}">${text}</div></div>`;
}
function showDbError(err) {
  toast(err.message || 'Ошибка доступа к базе');
  const node = $('#category-bars'); if (node) node.innerHTML = `<div class="empty-inline">${esc(err.message)}</div>`;
}

let searchTimer;
function productFilters() {
  return new URLSearchParams({q:$('#search-input').value, category:$('#category-filter').value, promo:$('#promo-filter').value, sort:$('#sort-filter').value, page:String(state.page), limit:'30'});
}
async function loadProducts() {
  try {
    const d = await api('/api/products?' + productFilters().toString());
    state.productPages = d.pages || 1; state.productTotal = d.total || 0;
    const categories = d.categories || [];
    const select = $('#category-filter');
    const old = select.value;
    if (select.options.length !== categories.length + 1) {
      select.innerHTML = '<option value="">Все категории</option>' + categories.map(c => `<option value="${esc(c)}">${esc(c)}</option>`).join('');
      select.value = categories.includes(old) ? old : '';
    }
    $('#products-total').textContent = `${num(d.total)} товаров`;
    $('#products-hint').textContent = `Страница ${d.page} из ${Math.max(1,d.pages)}`;
    $('#page-info').textContent = `Страница ${d.page} из ${Math.max(1,d.pages)}`;
    $('#prev-page').disabled = state.page <= 1; $('#next-page').disabled = state.page >= state.productPages;
    $('#products-body').innerHTML = d.items.length ? d.items.map(renderProductRow).join('') : '<tr><td colspan="8" class="empty-cell">Ничего не найдено. Измените фильтр или поисковый запрос.</td></tr>';
    $$('#products-body [data-product]').forEach(btn => btn.addEventListener('click', () => openProduct(btn.dataset.sap, btn.dataset.plu)));
  } catch (err) { $('#products-body').innerHTML = `<tr><td colspan="8" class="empty-cell">${esc(err.message)}</td></tr>`; }
}
function renderProductRow(p) {
  const image = p.local_image || p.image_url || '';
  const safe = safeUrl(image);
  const discount = p.discount_pct !== null && p.discount_pct !== undefined ? `<span class="discount-pill">−${Number(p.discount_pct).toLocaleString('ru-RU')}%</span>` : '—';
  return `<tr><td><div class="product-cell">${safe ? `<img class="product-thumb" src="${esc(safe)}" loading="lazy" alt="" onerror="this.style.visibility='hidden'">` : '<div class="product-thumb"></div>'}<div><div class="product-title">${esc(p.name || 'Без названия')}</div><div class="product-sub">${esc(p.categories || '')}</div></div></div></td><td>${esc(p.plu)}</td><td class="price-strong">${money(p.current_price)}</td><td class="price-muted">${p.regular_price ? money(p.regular_price) : '—'}</td><td>${discount}</td><td>${num(p.observations)}</td><td>${date(p.last_seen_at,false)}</td><td><button class="view-button" data-product data-sap="${esc(p.sap_code)}" data-plu="${esc(p.plu)}">Подробнее</button></td></tr>`;
}
['search-input','category-filter','promo-filter','sort-filter'].forEach(id => {
  const el = document.getElementById(id);
  el.addEventListener(id === 'search-input' ? 'input' : 'change', () => {
    state.page = 1;
    if (id === 'search-input') { clearTimeout(searchTimer); searchTimer = setTimeout(loadProducts, 280); }
    else loadProducts();
  });
});
$('#prev-page').addEventListener('click', () => { if (state.page > 1) { state.page--; loadProducts(); } });
$('#next-page').addEventListener('click', () => { if (state.page < state.productPages) { state.page++; loadProducts(); } });
$('#clear-filters').addEventListener('click', () => { $('#search-input').value=''; $('#category-filter').value=''; $('#promo-filter').value='all'; $('#sort-filter').value='name'; state.page=1; loadProducts(); });

async function loadChanges() {
  try {
    const d = await api('/api/changes?limit=500&direction=' + encodeURIComponent($('#change-direction').value));
    $('#changes-body').innerHTML = d.length ? d.map(c => {
      const up = Number(c.delta || 0) > 0;
      const delta = c.delta === null ? '—' : `<span class="${up?'recent-delta up':'recent-delta down'}">${up?'+':''}${money(c.delta)} ${c.delta_pct !== null ? `(${up?'+':''}${c.delta_pct}%)`:''}</span>`;
      return `<tr><td>${date(c.captured_at)}</td><td><div class="product-title">${esc(c.name || 'Товар')}</div><div class="product-sub">${esc(c.category || '')}</div></td><td>${esc(c.plu)}</td><td>${money(c.old_price)}</td><td class="price-strong">${money(c.new_price)}</td><td>${delta}</td><td>${esc(c.what_changed)}</td></tr>`;
    }).join('') : '<tr><td colspan="7" class="empty-cell">Изменений пока нет. Сделайте новый экспорт страницы позже, чтобы сравнить цены.</td></tr>';
  } catch (err) { $('#changes-body').innerHTML = `<tr><td colspan="7" class="empty-cell">${esc(err.message)}</td></tr>`; }
}
$('#change-direction').addEventListener('change', loadChanges);

async function loadCollection() {
  try {
    const [cats, exports, pending] = await Promise.all([api('/api/categories'), api('/api/exports'), api('/api/pending')]);
    $('#coverage-count').textContent = `${cats.filter(c => c.files>0).length} из ${cats.length} категорий с выгрузками`;
    $('#coverage-body').innerHTML = cats.length ? cats.map(c => {
      const status = c.files ? 'Собирается' : 'Не начато';
      const pages = c.pages?.length ? c.pages.join(', ') : '—';
      const miss = c.missing_pages_estimate?.length ? `<div class="coverage-missing">Не видны в базе страницы: ${c.missing_pages_estimate.join(', ')}</div>` : (c.max_page_linked ? `<div class="coverage-missing">В пагинации виден максимум: ${c.max_page_linked}</div>` : '');
      return `<tr><td><strong>${esc(c.category)}</strong></td><td>${num(c.unique_products)}</td><td>${pages}${miss}</td><td>${num(c.files)}</td><td>${date(c.last_capture,false)}</td><td><span class="status-chip ${c.files?'started':''}">${status}</span></td></tr>`;
    }).join('') : '<tr><td colspan="6" class="empty-cell">Категории появятся после импорта списка или первой выгрузки.</td></tr>';
    $('#exports-count').textContent = `${exports.length} файлов`;
    $('#run-import').disabled = pending.length === 0;
    $('#run-import').textContent = pending.length ? `Импортировать выгрузки (${pending.length})` : 'Нет новых выгрузок';
    if (pending.length && !$('#upload-result').textContent.trim()) $('#upload-result').hidden = true;
    $('#exports-body').innerHTML = exports.length ? exports.slice(0,40).map(e => `<tr><td title="${esc(e.file_name)}">${esc(e.file_name)}</td><td>${esc(e.category || '—')}</td><td>${esc(e.page_number || '—')}</td><td>${num(e.product_rows)}</td><td>${date(e.captured_at)}</td><td>${date(e.imported_at)}</td></tr>`).join('') : '<tr><td colspan="6" class="empty-cell">Файлов пока нет.</td></tr>';
  } catch (err) { $('#coverage-body').innerHTML = `<tr><td colspan="6" class="empty-cell">${esc(err.message)}</td></tr>`; }
  pollJobOnce();
}

function readFileBase64(file) {
  return new Promise((resolve,reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(',')[1] || '');
    reader.onerror = () => reject(new Error(`Не удалось прочитать ${file.name}`));
    reader.readAsDataURL(file);
  });
}
async function handleFiles(files) {
  const list = Array.from(files || []).filter(f => f.name.toLowerCase().endsWith('.json'));
  if (!list.length) return toast('Выберите JSON-файл из 5ka.ru.');
  try {
    const payloadFiles = [];
    for (const file of list) payloadFiles.push({name:file.name, content_base64:await readFileBase64(file)});
    const result = await api('/api/upload', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({files:payloadFiles})});
    const saved = result.saved || [];
    const products = saved.filter(x => x.kind === 'products').length;
    const cats = saved.filter(x => x.kind === 'categories').length;
    const details = saved.map(x => `${x.name}${x.products ? ` (${x.products} товаров)` : ''}`).join('\n');
    $('#upload-result').hidden = false;
    $('#upload-result').textContent = `Сохранено файлов: ${saved.length}; уже были: ${(result.skipped||[]).length}. Выгрузок товаров: ${products}; списков категорий: ${cats}. ${details}`;
    $('#run-import').disabled = products === 0;
    if (products) toast(`Добавлено файлов с товарами: ${products}. Запустите импорт.`);
    else toast('Список категорий сохранён. Товаров для импорта пока нет.');
    state.files = [];
    await loadCollection();
  } catch (err) { toast(err.message); }
}
$('#file-input').addEventListener('change', e => handleFiles(e.target.files));
const drop = $('#dropzone');
drop.addEventListener('dragover', e => {e.preventDefault();drop.classList.add('dragover');});
drop.addEventListener('dragleave', () => drop.classList.remove('dragover'));
drop.addEventListener('drop', e => {e.preventDefault();drop.classList.remove('dragover');handleFiles(e.dataTransfer.files);});
$('#run-import').addEventListener('click', async () => {
  try {
    await api('/api/import', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({download_photos:$('#download-photos').checked})});
    $('#job-card').hidden = false; $('#job-card').className='job-card'; $('#job-title').textContent='Импорт запущен'; $('#job-status').textContent='выполняется';
    $('#run-import').disabled = true; state.polling=true; pollJob();
  } catch (err) { toast(err.message); }
});
$('#open-exports-folder').addEventListener('click', async () => {
  try { const d=await api('/api/open-folder',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({kind:'exports'})}); toast('Открыта папка '+d.opened); }
  catch(err){toast(err.message);}
});
async function pollJobOnce() {
  try {
    const job=await api('/api/job');
    if (job.started_at) {
      $('#job-card').hidden=false;
      const done = !job.running && job.finished_at;
      $('#job-card').className='job-card'+(done && job.return_code===0?' done':done?' error':'');
      $('#job-title').textContent=job.running?'Импорт выполняется':done?'Импорт завершён':'Последний импорт';
      $('#job-status').textContent=job.running?'в работе':done?(job.return_code===0?'готово':'ошибка'):'';
      $('#job-output').textContent=job.error ? `${job.error}\n\n${job.output||''}` : (job.output||'');
      if (done) $('#run-import').disabled=false;
    }
  } catch (_) {}
}
async function pollJob() {
  while (state.polling) {
    await pollJobOnce();
    const job=await api('/api/job').catch(()=>({running:false}));
    if (!job.running) break;
    await new Promise(r=>setTimeout(r,1200));
  }
  state.polling=false; $('#run-import').disabled=false;
  await Promise.all([loadDashboard(),loadCollection()]);
  toast('Обновление базы завершено.');
}

async function openProduct(sap, plu) {
  $('#product-modal').hidden = false; $('#product-detail').innerHTML='<div class="empty-cell">Загружаем карточку…</div>';
  try {
    const d=await api(`/api/product?sap=${encodeURIComponent(sap)}&plu=${encodeURIComponent(plu)}`);
    const p=d.product, hist=d.history||[];
    const image = p.image_file ? `/photos/${encodeURIComponent(String(p.image_file).split(/[\\/]/).pop())}` : safeUrl(p.image_url);
    const discount = p.discount_pct!==null && p.discount_pct!==undefined ? `<span class="detail-discount">−${Number(p.discount_pct).toLocaleString('ru-RU')}%</span>` : '';
    const points = hist.filter(x=>x.current_price!==null && x.current_price!==undefined);
    const chart = drawHistoryChart(points);
    const historyHtml = hist.slice().reverse().slice(0,12).map(x=>`<div class="history-row"><span>${date(x.captured_at)}</span><span>${esc(x.category||'')}</span><strong>${money(x.current_price)} ${x.discount_pct!==null?`· −${x.discount_pct}%`:''}</strong></div>`).join('');
    const productUrl=safeUrl(p.product_url);
    $('#product-detail').innerHTML = `
      <div class="detail-top">${image?`<img class="detail-image" src="${esc(image)}" alt="" onerror="this.style.visibility='hidden'">`:'<div class="detail-image"></div>'}
        <div><div class="eyebrow green">SAP ${esc(p.sap_code)} · PLU ${esc(p.plu)}</div><h2 class="detail-title" id="modal-title">${esc(p.name||'Без названия')}</h2><div class="detail-meta">${esc(p.categories||'')}</div>
          <div class="detail-prices"><span class="detail-current">${money(p.current_price)}</span>${p.regular_price?`<span class="detail-old">${money(p.regular_price)}</span>`:''}${discount}</div>
          <div class="detail-dates"><span class="date-tag">Первое появление: ${date(p.first_seen_at)}</span><span class="date-tag">Последнее наблюдение: ${date(p.last_seen_at)}</span><span class="date-tag">Первый импорт: ${date(p.first_imported_at)}</span><span class="date-tag">Последний импорт: ${date(p.last_imported_at)}</span></div>
          <div class="detail-dates"><span class="date-tag">Наблюдений: ${hist.length}</span>${productUrl?`<a class="date-tag" href="${esc(productUrl)}" target="_blank" rel="noopener">Открыть на 5ka.ru ↗</a>`:''}</div>
        </div>
      </div>
      <div class="detail-section"><h3>История цены</h3><div class="chart-wrap">${chart}</div></div>
      <div class="detail-section"><h3>Последние наблюдения</h3><div class="history-list">${historyHtml||'<div class="empty-inline">История пока пуста.</div>'}</div></div>`;
  } catch(err) { $('#product-detail').innerHTML=`<div class="empty-cell">${esc(err.message)}</div>`; }
}
function drawHistoryChart(points) {
  const W=760,H=190, L=44,R=18,T=16,B=32;
  if (!points.length) return '<div class="empty-inline">Нет числовых наблюдений для графика.</div>';
  if (points.length===1) return `<svg class="history-chart" viewBox="0 0 ${W} ${H}" role="img"><text x="${W/2}" y="85" text-anchor="middle" fill="#87968a" font-size="12">Один снимок: ${money(points[0].current_price)}</text><circle cx="${W/2}" cy="${H/2}" r="5" fill="#4d9560"/></svg>`;
  const vals=points.map(p=>Number(p.current_price)), min=Math.min(...vals), max=Math.max(...vals), span=(max-min)||1;
  const x=i=>L+i*(W-L-R)/(points.length-1), y=v=>T+(max-v)*(H-T-B)/span;
  const path=points.map((p,i)=>`${i?'L':'M'} ${x(i).toFixed(1)} ${y(Number(p.current_price)).toFixed(1)}`).join(' ');
  const dots=points.map((p,i)=>`<circle cx="${x(i)}" cy="${y(Number(p.current_price))}" r="3.7" fill="#4d9560"><title>${date(p.captured_at)}: ${money(p.current_price)}</title></circle>`).join('');
  const yTop=y(max),yBottom=y(min);
  return `<svg class="history-chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="История цены"><line x1="${L}" y1="${yTop}" x2="${W-R}" y2="${yTop}" stroke="#e9eee8"/><line x1="${L}" y1="${yBottom}" x2="${W-R}" y2="${yBottom}" stroke="#e9eee8"/><text x="${L-8}" y="${yTop+4}" text-anchor="end" fill="#8b998e" font-size="10">${max.toFixed(2)}</text><text x="${L-8}" y="${yBottom+4}" text-anchor="end" fill="#8b998e" font-size="10">${min.toFixed(2)}</text><path d="${path}" fill="none" stroke="#4d9560" stroke-width="2.7" stroke-linecap="round" stroke-linejoin="round"/>${dots}<text x="${L}" y="${H-8}" fill="#8b998e" font-size="9">${date(points[0].captured_at,false)}</text><text x="${W-R}" y="${H-8}" text-anchor="end" fill="#8b998e" font-size="9">${date(points.at(-1).captured_at,false)}</text></svg>`;
}
$('#modal-close').addEventListener('click',()=>$('#product-modal').hidden=true);
$('#product-modal').addEventListener('click',e=>{if(e.target===$('#product-modal')) $('#product-modal').hidden=true;});
document.addEventListener('keydown',e=>{if(e.key==='Escape') $('#product-modal').hidden=true;});

$$('[data-view]').forEach(b=>b.addEventListener('click',()=>setView(b.dataset.view)));
$$('[data-goto]').forEach(b=>b.addEventListener('click',()=>setView(b.dataset.goto)));
$('#refresh-button').addEventListener('click',()=>{ if(state.view==='dashboard') loadDashboard(); else if(state.view==='products') loadProducts(); else if(state.view==='changes') loadChanges(); else loadCollection(); toast('Данные обновлены'); });
$('#mobile-menu').addEventListener('click',()=>$('#sidebar').classList.toggle('open'));

(async function boot(){
  const health=await api('/api/health').catch(err=>({ok:false,error:err.message}));
  if(!health.ok){toast(health.error||'Не удалось подключиться к базе');}
  loadDashboard();
  loadCollection();
})();
