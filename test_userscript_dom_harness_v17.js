'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');
const { parseHTML } = require('linkedom');

const ROOT = __dirname;
const FIXTURES = path.join(ROOT, 'tests', 'fixtures');

function addOutsidePageChrome(html) {
  return html.replace('</body>', [
    '<section><h2>Вам понравится</h2><article><h3>Совершенно другой товар</h3>',
    '<img alt="Рекомендованный товар" src="https://catalog-images.x5static.net/product/recommendation.jpeg">',
    '</article></section>',
    '<section><h2>Отзывы покупателей</h2><p>Отзыв о другом товаре</p></section>',
    '</body>'
  ].join(''));
}

function hiddenByStyle(element) {
  for (let node = element; node && node.nodeType === 1; node = node.parentElement) {
    const style = node.getAttribute('style') || '';
    if (node.hasAttribute('hidden') ||
        /display\s*:\s*none/i.test(style) ||
        /visibility\s*:\s*hidden/i.test(style) ||
        /opacity\s*:\s*0(?:\s*;|\s*$)/i.test(style)) return true;
  }
  return false;
}

function renderInnerText(node) {
  if (node.nodeType === 3) return hiddenByStyle(node.parentElement) ? '' : (node.textContent || '');
  if (node.nodeType !== 1 || hiddenByStyle(node)) return '';
  const block = /^(ADDRESS|ARTICLE|ASIDE|BLOCKQUOTE|BR|DIV|DL|DT|DD|FIELDSET|FIGCAPTION|FIGURE|FOOTER|FORM|H1|H2|H3|H4|HEADER|LI|MAIN|NAV|OL|P|SECTION|TABLE|TR|UL)$/.test(node.tagName);
  const children = [...node.childNodes].map(renderInnerText).join('');
  return block ? '\n' + children + '\n' : children;
}

function loadUserscript(relativePath, html, chain, plu) {
  const parsed = parseHTML(html);
  const document = parsed.document || parsed;
  const window = parsed.window || document.defaultView || parsed;
  const elementPrototype = window.HTMLElement?.prototype || window.Element.prototype;

  Object.defineProperty(elementPrototype, 'innerText', {
    configurable: true,
    get() { return renderInnerText(this).replace(/\n[ \t]+/g, '\n').replace(/\n{2,}/g, '\n').trim(); }
  });

  window.getComputedStyle = element => {
    const style = element.getAttribute('style') || '';
    const display = /display\s*:\s*none/i.test(style) ? 'none' : 'block';
    const visibility = /visibility\s*:\s*hidden/i.test(style) ? 'hidden' : 'visible';
    const opacityMatch = style.match(/opacity\s*:\s*([0-9.]+)/i);
    return { display, visibility, opacity: opacityMatch ? opacityMatch[1] : '1' };
  };
  window.scrollY = 0;

  window.Element.prototype.getBoundingClientRect = function () {
    if (hiddenByStyle(this)) return { x: 0, y: 0, top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0 };
    const productRoot = document.querySelector('[data-qa="product-screen"]');
    const isRecommendation = /^(вам понравится|похожие товары|с этим товаром покупают|рекомендуем|популярные товары|вам также понравится)/i.test(
      (this.innerText || '').trim()
    );
    const top = isRecommendation ? 1000 : (productRoot && productRoot.contains(this) ? 100 : 0);
    return { x: 0, y: top, top, left: 0, right: 160, bottom: top + 160, width: 160, height: 160 };
  };

  const origin = chain === '5ka' ? 'https://5ka.ru' : 'https://www.perekrestok.ru';
  const href = origin + '/product/test--' + plu;
  const context = {
    document,
    window,
    location: { origin, href, pathname: '/product/test--' + plu },
    URL,
    URLSearchParams,
    Date,
    console,
    getComputedStyle: window.getComputedStyle,
    module: { exports: {} }
  };
  const source = fs.readFileSync(path.join(ROOT, relativePath), 'utf8');
  vm.runInNewContext(source, context, { filename: relativePath });
  return { parser: context.module.exports, document };
}

function readFixture(name) {
  return fs.readFileSync(path.join(FIXTURES, name), 'utf8');
}

test('Pyaterochka real DOM builds a scoped payload with clean description and composition', () => {
  const html = addOutsidePageChrome(readFixture('5ka_4275044.html'));
  const { parser } = loadUserscript('ui_v14/5ka_batch_enrichment_v16.user.js', html, '5ka', '4275044');
  const scoped = parser.productRootAndText();
  assert.ok(scoped.main.matches('[data-qa="product-screen"]'));
  assert.doesNotMatch(scoped.productText, /Совершенно другой товар|Отзывы покупателей/);

  const payload = parser.buildPayload('35XY');
  assert.equal(payload.source, '5ka.ru-visible-product-details-export');
  assert.equal(payload.store.sap_code, '35XY');
  assert.equal(payload.product.plu, '4275044');
  assert.equal(payload.product.description,
    'Агар Dr. Bakers Professional обладает оптимальной прочностью геля* и не требует длительного кипячения для его образования. Идеально подходит для приготовления мармелада, суфле, зефира и других изделий.');
  assert.equal(payload.product.composition,
    'агар (загуститель, агент желирующий). Может содержать следы молока, глютена, горчицы, сои, яиц, кунжута и орехов');
  assert.deepEqual({ ...payload.product.nutrition }, {
    Белки: '0.4', Жиры: '0.0', Углеводы: '0.0', Ккал: '174.0'
  });
  assert.equal(payload.product.attributes['Вес'], '0.007 кг');
  assert.equal(payload.product.attributes['Срок хранения'], '365 дней');
  assert.equal(payload.product.details_text.includes('Отзывы покупателей'), false);
});

test('Pyaterochka collapsed description is not reported as captured', () => {
  const html = readFixture('5ka_4275044.html').replace(
    '<div><h2>Описание</h2>',
    '<div style="display:none"><h2>Описание</h2>'
  );
  const { parser } = loadUserscript('ui_v14/5ka_batch_enrichment_v16.user.js', html, '5ka', '4275044');
  const payload = parser.buildPayload('35XY');
  assert.equal(payload.product.description, null);
  assert.equal(payload.product.details_text.includes('оптимальной прочностью геля'), false);
});

test('Perekrestok non-food real DOM routes generic composition to materials', () => {
  const html = addOutsidePageChrome(readFixture('perekrestok_4414429.html'));
  const { parser } = loadUserscript('ui_v14/pr_batch_enrichment_v16.user.js', html, 'perekrestok', '4414429');
  const scoped = parser.productRootAndText();
  assert.ok(scoped.main.matches('[data-qa="product-screen"]'));
  assert.doesNotMatch(scoped.productText, /Совершенно другой товар|Отзывы покупателей/);

  const payload = parser.buildPayload('XSEO');
  assert.equal(payload.chain, 'perekrestok');
  assert.equal(payload.product.plu, '4414429');
  assert.equal(payload.product.product_class, 'non_food');
  assert.equal(payload.product.composition, null);
  assert.equal(payload.product.materials, 'Полиэтилен, джут.');
  assert.equal(payload.product.attributes['Вес'], '0.37 кг');
  assert.equal(payload.product.attributes['Срок хранения'], 'не ограничен');
  assert.equal(payload.product.details_text.includes('Отзывы покупателей'), false);
});

test('Perekrestok visible description is captured, collapsed description is not', () => {
  const fixture = readFixture('perekrestok_4414429.html');
  const marker = '</div>\n</body>';
  assert.ok(fixture.includes(marker), 'fixture closing marker changed');
  const visibleHtml = fixture.replace(marker,
    '<div data-qa="product-description">Фактическое описание товара для DOM-теста.</div></div>\n</body>');
  const visibleParser = loadUserscript('ui_v14/pr_batch_enrichment_v16.user.js', visibleHtml, 'perekrestok', '4414429').parser;
  assert.equal(visibleParser.buildPayload('XSEO').product.description,
    'Фактическое описание товара для DOM-теста.');

  const hiddenHtml = visibleHtml.replace(
    '<div data-qa="product-description">',
    '<div data-qa="product-description" style="display:none">'
  );
  const hiddenParser = loadUserscript('ui_v14/pr_batch_enrichment_v16.user.js', hiddenHtml, 'perekrestok', '4414429').parser;
  assert.equal(hiddenParser.buildPayload('XSEO').product.description, null);
});

test('Perekrestok hidden SEO meta description is not treated as product description', () => {
  const html = readFixture('perekrestok_4406119.html');
  const parser = loadUserscript('ui_v14/pr_batch_enrichment_v16.user.js', html, 'perekrestok', '4406119').parser;
  const payload = parser.buildPayload('XSEO');
  assert.equal(payload.product.description, null);
  assert.equal(payload.product.product_class, 'food');
  assert.equal(payload.product.nutrition['Ккал'], '52.7');
  assert.equal(payload.product.details_text.includes('Вам понравится'), false);
});

for (const [script, relativePath] of [
  ['Pyaterochka', 'ui_v14/5ka_batch_enrichment_v16.user.js'],
  ['Perekrestok', 'ui_v14/pr_batch_enrichment_v16.user.js']
]) {
  test(script + ' composition parser requires a standalone section label and keeps Cyrillic words intact', () => {
    const chain = script === 'Pyaterochka' ? '5ka' : 'perekrestok';
    const { parser } = loadUserscript(relativePath,
      '<!doctype html><html><body><div data-qa="product-screen"><h1 data-qa="product-card-title">Тест</h1></div></body></html>',
      chain, '123');
    assert.equal(parser.extractComposition('Описание\nВ составе продукта есть древесина.\nВес\n0.37 кг'), null);
    assert.equal(parser.extractComposition('Описание\nВ составе продукта есть компоненты.\nСостав\nДревесина, джут.\nВес\n0.37 кг'),
      'Древесина, джут.');
    assert.equal(parser.extractComposition('Состав: яблоко, вода\nВес\n150 г'), 'яблоко, вода');
  });
}
