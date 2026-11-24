/*
 * Экспорт списка категорий, уже отображённых на странице каталога 5ka.ru.
 * Не обращается к API, не делает запросов, не решает CAPTCHA.
 */
(() => {
  "use strict";
  const clean = (value) => String(value || "").replace(/\s+/g, " ").trim();
  const rows = [];
  const seen = new Set();
  for (const a of document.querySelectorAll('a[href*="/catalog/"]')) {
    let url;
    try { url = new URL(a.getAttribute("href"), location.href); } catch (_) { continue; }
    const m = url.pathname.match(/--([A-Za-z0-9]{6,})(?:\/|$)/);
    if (!m || seen.has(m[1])) continue;
    seen.add(m[1]);
    rows.push({ category_id: m[1], name: clean(a.innerText || a.getAttribute("aria-label") || a.title), url: url.href });
  }
  if (!rows.length) {
    alert("Не найдены ссылки на категории. Откройте https://5ka.ru/catalog/ и повторите.");
    return;
  }
  const payload = {
    schema_version: 1,
    source: "5ka.ru-visible-category-links",
    captured_at: new Date().toISOString(),
    source_url: location.href,
    categories: rows
  };
  const slug = new Date().toISOString().replace(/[:.]/g, "-");
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json;charset=utf-8" });
  const blobUrl = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = blobUrl;
  a.download = `5ka_categories_${slug}.json`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(blobUrl), 1500);
  alert(`Сохранено ссылок на категорий: ${rows.length}. Файл 5ka_categories_*.json скачан.`);
})();
