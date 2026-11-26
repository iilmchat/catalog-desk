'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const test = require('node:test');
const { chromium } = require('playwright');

const ROOT = __dirname;
const FIXTURES = path.join(ROOT, 'tests', 'fixtures');
const SCRIPTS = {
  perekrestok: path.join(ROOT, 'ui_v14', 'pr_batch_enrichment_v16.user.js'),
  '5ka': path.join(ROOT, 'ui_v14', '5ka_batch_enrichment_v16.user.js')
};

let server;
let origin;
let browser;

test.before(async () => {
  server = http.createServer((req, res) => {
    const plu = new URL(req.url, 'http://127.0.0.1').pathname.match(/--(\d+)$/)?.[1];
    const fixture = plu === '4414429'
      ? 'perekrestok_4414429.html'
      : plu === '4275044' ? '5ka_4275044.html' : null;
    if (!fixture) {
      res.writeHead(404);
      res.end('fixture not found');
      return;
    }
    res.writeHead(200, { 'content-type': 'text/html; charset=utf-8' });
    fs.createReadStream(path.join(FIXTURES, fixture)).pipe(res);
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  origin = 'http://127.0.0.1:' + server.address().port;
  browser = await chromium.launch({ headless: true });
});

test.after(async () => {
  if (browser) await browser.close();
  if (server) await new Promise(resolve => server.close(resolve));
});

async function openFixture(plu, retailer) {
  const page = await browser.newPage();
  // The test is deliberately offline: only the local fixture server is allowed.
  await page.route('**/*', route => {
    const url = new URL(route.request().url());
    return url.hostname === '127.0.0.1'
      ? route.continue()
      : route.abort();
  });
  await page.goto(origin + '/product/test--' + plu, { waitUntil: 'domcontentloaded' });
  const source = fs.readFileSync(SCRIPTS[retailer], 'utf8');
  await page.addScriptTag({
    content: 'window.__catalogDesk = (() => { const module = { exports: {} }; ' +
      source + '\n; return module.exports; })();'
  });
  return page;
}

test('Chromium: Perekrestok payload uses real browser DOM and keeps non-food materials separate', async () => {
  const page = await openFixture('4414429', 'perekrestok');
  try {
    const result = await page.evaluate(() => {
      const parser = window.__catalogDesk;
      const scoped = parser.productRootAndText();
      const payload = parser.buildPayload('XSEO');
      return {
        hasProductRoot: scoped.main.matches('[data-qa="product-screen"]'),
        className: payload.product.product_class,
        composition: payload.product.composition,
        materials: payload.product.materials,
        weight: payload.product.attributes['Вес'],
        detailsText: payload.product.details_text
      };
    });
    assert.equal(result.hasProductRoot, true);
    assert.equal(result.className, 'non_food');
    assert.equal(result.composition, null);
    assert.equal(result.materials, 'Полиэтилен, джут.');
    assert.equal(result.weight, '0.37 кг');
    assert.doesNotMatch(result.detailsText, /Отзывы покупателей|Совершенно другой товар/);
  } finally {
    await page.close();
  }
});

test('Chromium: visible description is captured only after the page expands it', async () => {
  const page = await openFixture('4414429', 'perekrestok');
  try {
    await page.evaluate(() => {
      const root = document.querySelector('[data-qa="product-screen"]');
      const button = document.createElement('button');
      button.id = 'expand-description';
      button.textContent = 'Показать полностью';
      const description = document.createElement('div');
      description.dataset.qa = 'product-description';
      description.hidden = true;
      description.textContent = 'Проверяемое описание из раскрытой секции карточки.';
      button.addEventListener('click', () => { description.hidden = false; });
      root.append(button, description);
    });

    const before = await page.evaluate(() => window.__catalogDesk.buildPayload('XSEO').product.description);
    assert.equal(before, null);

    await page.locator('#expand-description').click();
    const after = await page.evaluate(() => ({
      visible: (() => {
        const node = document.querySelector('[data-qa="product-description"]');
        const rect = node.getBoundingClientRect();
        return rect.width > 0 && rect.height > 0 && getComputedStyle(node).display !== 'none';
      })(),
      description: window.__catalogDesk.buildPayload('XSEO').product.description
    }));
    assert.equal(after.visible, true);
    assert.equal(after.description, 'Проверяемое описание из раскрытой секции карточки.');
  } finally {
    await page.close();
  }
});

test('Chromium: Pyaterochka description is present in the actual rendered text and payload', async () => {
  const page = await openFixture('4275044', '5ka');
  try {
    const result = await page.evaluate(() => {
      const parser = window.__catalogDesk;
      const scoped = parser.productRootAndText();
      const payload = parser.buildPayload('35XY');
      return {
        hasProductRoot: scoped.main.matches('[data-qa="product-screen"]'),
        renderedText: scoped.main.innerText,
        description: payload.product.description,
        plu: payload.product.plu,
        nutrition: payload.product.nutrition
      };
    });
    assert.equal(result.hasProductRoot, true);
    assert.match(result.renderedText, /оптимальной прочностью геля/);
    assert.match(result.description, /оптимальной прочностью геля/);
    assert.equal(result.plu, '4275044');
    assert.equal(result.nutrition['Ккал'], '174.0');
  } finally {
    await page.close();
  }
});
