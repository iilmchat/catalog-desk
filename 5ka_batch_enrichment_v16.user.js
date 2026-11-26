// ==UserScript==
// @name         Catalog Desk — 5ka details V16
// @namespace    local.pyaterochka.catalogdesk
// @version      16.0.0
// @description  V16 single-card refresh and batch capture from visible 5ka.ru DOM only; stops on CAPTCHA.
// @match        https://5ka.ru/*
// @match        https://www.5ka.ru/*
// @run-at       document-idle
// @grant        GM_xmlhttpRequest
// @connect      127.0.0.1
// @connect      localhost
// ==/UserScript==

(function () {
  'use strict';

  const LOCAL_API = 'http://127.0.0.1:8767';
  const CHAIN = '5ka';
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  const clean = value => String(value ?? '').replace(/\u00a0/g, ' ').replace(/\s+/g, ' ').trim();
  const visible = element => {
    if (!element) return false;
    const rect = element.getBoundingClientRect();
    const style = getComputedStyle(element);
    return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden' && Number(style.opacity || 1) > 0;
  };

  function request(method, path, data) {
    return new Promise((resolve, reject) => {
      GM_xmlhttpRequest({
        method,
        url: LOCAL_API + path,
        headers: data ? { 'Content-Type': 'application/json; charset=utf-8' } : {},
        data: data ? JSON.stringify(data) : undefined,
        timeout: 30000,
        onload: response => {
          let body;
          try { body = JSON.parse(response.responseText || '{}'); }
          catch (_) { reject(new Error('Локальный сервер вернул некорректный JSON')); return; }
          if (response.status < 200 || response.status >= 300) reject(new Error(body.error || `HTTP ${response.status}`));
          else resolve(body);
        },
        onerror: () => reject(new Error('Не удалось связаться с Catalog Desk на 127.0.0.1:8767')),
        ontimeout: () => reject(new Error('Истекло время ожидания локального сервера'))
      });
    });
  }

  function showNotice(message, isError = false) {
    let box = document.getElementById('py5ka-v16-notice');
    if (!box) {
      box = document.createElement('div');
      box.id = 'py5ka-v16-notice';
      box.style.cssText = 'position:fixed;z-index:2147483647;right:18px;bottom:18px;max-width:430px;padding:14px 18px;border-radius:12px;background:#173b2a;color:#fff;font:14px/1.5 Arial,sans-serif;box-shadow:0 8px 28px #0004;border:1px solid #7da46c;';
      document.documentElement.appendChild(box);
    }
    box.style.background = isError ? '#7b302b' : '#173b2a';
    box.textContent = message;
  }

  function isChallengePage() {
    const haystack = `${location.pathname} ${document.title} ${document.body?.innerText || ''}`.toLowerCase();
    return /xpvnsulc|пройдите проверку|убедиться, что имеем дело|а не с ботом|похоже на поведение автоматизированных систем/.test(haystack);
  }

  function recommendationTop() {
    const pattern = /^(вам понравится|похожие товары|с этим товаром покупают|рекомендуем|популярные товары|вам также понравится)/i;
    const headings = [...document.querySelectorAll('h1,h2,h3,[role="heading"]')]
      .filter(visible)
      .filter(el => pattern.test(clean(el.innerText)));
    if (!headings.length) return Infinity;
    return Math.min(...headings.map(el => el.getBoundingClientRect().top + window.scrollY));
  }

  function similarity(a, b) {
    const stop = new Set(['для', 'из', 'и', 'с', 'со', 'на', 'в', 'по', 'от', 'к', 'г', 'кг', 'мл', 'шт']);
    const tokens = value => clean(value).toLowerCase().replace(/[^\p{L}\p{N}]+/gu, ' ').split(' ').filter(x => x.length > 2 && !stop.has(x));
    const aa = [...new Set(tokens(a))], bb = [...new Set(tokens(b))];
    if (!aa.length || !bb.length) return 0;
    const matches = aa.filter(token => bb.includes(token)).length;
    return matches / Math.min(aa.length, bb.length);
  }

  function productRootAndText() {
    const main = document.querySelector('main,[role="main"]') || document.body;
    const title = main.querySelector('h1') || document.querySelector('h1');
    const fullText = String(main.innerText || main.textContent || '');
    const recHeading = /(?:^|\n)\s*(?:Вам понравится|Похожие товары|С этим товаром покупают|Рекомендуем|Популярные товары|Вам также понравится)\s*(?:\n|$)/i;
    const recIndex = fullText.search(recHeading);
    const productText = recIndex > 0 ? fullText.slice(0, recIndex) : fullText;
    return { main, title, fullText, productText };
  }

  function extractGallery(main, productName) {
    const recTop = recommendationTop();
    const candidates = [...main.querySelectorAll('img')].filter(visible).map(img => {
      const rect = img.getBoundingClientRect();
      return {
        url: img.currentSrc || img.src || img.getAttribute('data-src') || img.getAttribute('data-lazy-src'),
        alt: clean(img.alt || img.getAttribute('aria-label') || ''),
        area: rect.width * rect.height,
        y: rect.top + window.scrollY,
        width: rect.width,
        height: rect.height
      };
    }).filter(item => {
      if (!item.url || !/^https:\/\/catalog-images\.x5static\.net\/product\//i.test(item.url)) return false;
      if (item.width < 70 || item.height < 70) return false;
      if (item.y >= recTop) return false;
      return true;
    }).sort((a, b) => b.area - a.area);
    // Prefer only images whose alt text identifies THIS product. If the page
    // provides no alt at all, keep just the largest hero image as a safe fallback.
    const matched = candidates.filter(item => item.alt && similarity(productName, item.alt) >= 0.34);
    const chosen = matched.length ? matched : candidates.filter(item => !item.alt).slice(0, 1);
    const seen = new Set();
    return chosen.filter(item => {
      const key = item.url.split('?')[0];
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    }).slice(0, 12).map((item, index) => ({ url: item.url, alt: item.alt || productName, position: index + 1 }));
  }

  function parseNutrition(productText) {
    const start = productText.search(/Пищевая\s+ценность/i);
    let block = start >= 0 ? productText.slice(start, start + 420) : productText.slice(0, 600);
    const compositionAt = block.search(/\bСостав\b/i);
    if (compositionAt > 0) block = block.slice(0, compositionAt);
    const denominator = block.match(/(?:на\s*)?100\s*(?:г|мл)\b/i);
    if (denominator && denominator.index !== undefined) block = block.slice(denominator.index + denominator[0].length);
    const result = {};
    const specs = [
      ['Белки', /белки/i],
      ['Жиры', /жиры/i],
      ['Углеводы', /углеводы/i],
      ['Ккал', /(?:ккал|калорийность|энергетическая\s+ценность)/i]
    ];
    const firstLabel = /(?:белки|жиры|углеводы|ккал)\b/i.exec(block);
    const firstValue = /[0-9]+(?:[.,][0-9]+)?/.exec(block);
    const valueFirst = !!(firstLabel && firstValue && firstValue.index < firstLabel.index);
    for (const [key, label] of specs) {
      const labelText = label.source;
      const before = new RegExp(`([0-9]+(?:[.,][0-9]+)?)\\s*(?:г|g)?\\s*${labelText}\\b`, 'i');
      const after = new RegExp(`${labelText}\\s*[:：-]?\\s*([0-9]+(?:[.,][0-9]+)?)`, 'i');
      const match = valueFirst ? (block.match(before) || block.match(after)) : (block.match(after) || block.match(before));
      if (match) result[key] = match[1].replace(',', '.');
    }
    return result;
  }

  function extractComposition(productText) {
    const match = productText.match(/(?:^|\\n)\\s*Состав\\s*[:：]?\\s*([\\s\\S]{3,900}?)(?=\\n\\s*(?:Показать\\s*полностью|Вес|Масса нетто|Объ[её]м|Срок хранения|Срок годности|Условия хранения|Бренд|Страна производства|Производитель|Тип продукта|Добавки|Острый|Вид упаковки|Назначение соуса)\\b|$)/i);
    return match ? clean(match[1]) : null;
  }

  function extractDescription(productText) {
    const lines = String(productText || '').split(/\\r?\\n/).map(clean).filter(Boolean);
    const start = lines.findIndex(line => /^(?:Описание(?: товара)?|Описание товара[:：])$/i.test(line));
    if (start < 0) return null;
    const parts = [];
    for (const line of lines.slice(start + 1)) {
      if (/^(?:Состав|Пищевая ценность|Характеристики|Вес|Масса нетто|Объ[её]м|Срок хранения|Срок годности|Условия хранения|Бренд|Страна производства|Производитель|Показать полностью)$/i.test(line)) break;
      parts.push(line);
      if (parts.join(' ').length >= 1200) break;
    }
    const value = clean(parts.join(' '));
    return value.length >= 3 ? value.slice(0, 1200) : null;
  }

  function extractAttributes(productText, nutrition) {
    const labels = [
      'Вес', 'Масса нетто', 'Объем', 'Объём', 'Срок хранения', 'Срок годности',
      'Условия хранения', 'Температура хранения', 'Бренд', 'Страна производства',
      'Производитель', 'Тип продукта', 'Добавки', 'Острый', 'Вид упаковки',
      'Назначение соуса'
    ];
    const attrs = {};
    const lines = productText.split(/\r?\n/).map(clean).filter(Boolean);
    for (let i = 0; i < lines.length; i++) {
      const key = lines[i].replace(/[:：]$/, '');
      if (labels.some(label => label.toLowerCase() === key.toLowerCase()) && lines[i + 1]) attrs[key] = lines[i + 1];
      const pair = lines[i].match(/^([^:：]{2,42})[:：]\s*(.{1,220})$/);
      if (pair && labels.some(label => label.toLowerCase() === pair[1].trim().toLowerCase())) attrs[pair[1].trim()] = pair[2].trim();
    }
    for (const [key, value] of Object.entries(nutrition)) attrs[key] = `${value}${key === 'Ккал' ? ' ккал' : ' г'}`;
    return attrs;
  }

  function buildPayload(sap) {
    const { main, title, productText } = productRootAndText();
    if (!title) throw new Error('На странице не найден заголовок товара (H1).');
    const name = clean(title.innerText || document.title);
    const match = location.pathname.match(/--(\d+)(?:\/|$)/);
    if (!match) throw new Error('Не удалось определить PLU товара из URL.');
    const plu = match[1];
    const gallery = extractGallery(main, name);
    const nutrition = parseNutrition(productText);
    const composition = extractComposition(productText);
    const description = extractDescription(productText);
    const attributes = extractAttributes(productText, nutrition);
    const imageUrl = gallery[0]?.url || null;
    return {
      schema_version: 1,
      source: '5ka.ru-visible-product-details-export',
      captured_at: new Date().toISOString(),
      source_url: location.href,
      store: { sap_code: sap, name: null },
      collection_mode: 'batch-visible-current-page-dom-only',
      note: 'Собраны только сведения из открытой видимой страницы товара. Сетевые/API-запросы к 5ka.ru не выполнялись.',
      product: {
        plu,
        name,
        product_url: location.href,
        image_url: imageUrl,
        image_alt: gallery[0]?.alt || name,
        image_gallery: gallery,
        composition,
        description,
        nutrition,
        attributes,
        details_text: clean(productText)
      }
    };
  }


  async function runSingleRefresh(token) {
    if (isChallengePage()) {
      showNotice('Одиночное обновление остановлено: сайт запросил проверку. Пройдите её вручную и повторите действие.', true);
      return;
    }
    try {
      const requestInfo = await request('GET', '/api/details/refresh/claim?token=' + encodeURIComponent(token));
      if (requestInfo.chain !== CHAIN) throw new Error('Токен предназначен для другой торговой сети.');
      const expectedPath = new URL(requestInfo.product_url).pathname.replace(/\/+$/, '');
      const actualPath = location.pathname.replace(/\/+$/, '');
      if (expectedPath !== actualPath) throw new Error('URL карточки не совпадает с одноразовым запросом.');
      const pluMatch = location.pathname.match(/--(\d+)(?:\/|$)/);
      if (!pluMatch || String(requestInfo.plu) !== pluMatch[1]) {
        throw new Error('Открытая карточка не совпадает с PLU одноразового запроса.');
      }
      await sleep(3500);
      if (isChallengePage()) {
        showNotice('Одиночное обновление остановлено: требуется ручная проверка сайта. После проверки повторите действие в Catalog Desk.', true);
        return;
      }
      const payload = buildPayload(String(requestInfo.sap_code));
      const cleanUrl = location.origin + location.pathname + location.search;
      payload.collection_mode = 'single-visible-current-page-dom-only';
      payload.source_url = cleanUrl;
      if (payload.product) payload.product.product_url = cleanUrl;
      const result = await request('POST', '/api/details/refresh/record', { token, payload });
      history.replaceState(null, '', cleanUrl);
      showNotice('Сведения PLU ' + result.plu + ' обновлены. Массовая очередь не затронута.');
    } catch (error) {
      showNotice('Одиночное обновление не выполнено: ' + (error.message || error), true);
    }
  }

  async function main() {
    const refreshToken = new URLSearchParams(location.hash.slice(1)).get('catalog-desk-refresh');
    if (refreshToken) {
      await runSingleRefresh(refreshToken);
      return;
    }
    let state;
    try { state = await request('GET', '/api/batch/status'); }
    catch (_) { return; }

    if (isChallengePage()) {
      if (state.active) {
        await request('POST', '/api/batch/fail', { captcha: true, reason: 'На открытой странице обнаружена проверка X5.' }).catch(() => {});
        showNotice('Пакет остановлен: сайт запросил проверку. Пройди её вручную, затем нажми «Продолжить» в Catalog Desk.', true);
      }
      return;
    }

    const pluMatch = location.pathname.match(/--(\d+)(?:\/|$)/);
    if (!state.active || !pluMatch || String(state.current_plu || '') !== pluMatch[1]) return;

    showNotice(`Пакет V16: загружаю PLU ${pluMatch[1]}…`);
    await sleep(3500);
    if (isChallengePage()) {
      await request('POST', '/api/batch/fail', { captcha: true, reason: 'Проверка X5 появилась во время загрузки товара.' }).catch(() => {});
      showNotice('Пакет остановлен: нужна ручная проверка сайта.', true);
      return;
    }

    const guardKey = `py5ka-v16-done-${pluMatch[1]}`;
    if (sessionStorage.getItem(guardKey)) return;

    try {
      const current = await request('GET', '/api/batch/status');
      if (!current.active || String(current.current_plu || '') !== pluMatch[1]) return;
      const payload = buildPayload(String(current.current_sap || '35XY'));
      const result = await request('POST', '/api/batch/record', { payload });
      sessionStorage.setItem(guardKey, '1');
      if (result.batch_finished || !result.next_url) {
        showNotice(`Пакет завершён. Обработано в этом пакете: ${result.processed_count}. Вернись в Catalog Desk.`);
        return;
      }
      const delay = Math.max(12, Number(result.delay_seconds || 15));
      showNotice(`PLU ${pluMatch[1]} сохранён. Следующий товар через ${delay} сек. Прогресс: ${result.processed_count}.`);
      await sleep(delay * 1000);
      const latest = await request('GET', '/api/batch/status');
      if (!latest.active) {
        showNotice('Пакет приостановлен. Новая карточка не открыта.');
        return;
      }
      location.assign(result.next_url);
    } catch (error) {
      await request('POST', '/api/batch/fail', { captcha: false, reason: String(error.message || error) }).catch(() => {});
      showNotice(`Пакет остановлен: ${error.message || error}. Проверь локальный сервер и карточку товара.`, true);
    }
  }

  main();
})();
