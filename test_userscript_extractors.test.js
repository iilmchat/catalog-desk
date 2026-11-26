'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');

const root = __dirname;
const scripts = [
  'ui_v14/5ka_batch_enrichment_v16.user.js',
  'ui_v14/pr_batch_enrichment_v16.user.js'
];

function loadParser(relativePath, productText) {
  const source = fs.readFileSync(path.join(root, relativePath), 'utf8');
  const marker = '  main();\n})();';
  assert.ok(source.includes(marker), relativePath + ': main entrypoint marker missing');
  const fixtureMain = {
    innerText: productText,
    textContent: productText,
    querySelector(selector) {
      return selector === 'h1' ? { innerText: 'Тестовый товар' } : null;
    }
  };
  const context = {
    document: {
      body: fixtureMain,
      title: 'Тестовый товар',
      querySelector(selector) {
        return selector === 'main,[role="main"]' ? fixtureMain : null;
      },
      querySelectorAll() { return []; }
    },
    location: { pathname: '/product/test--123456', href: 'https://example.test/product/test--123456' },
    window: { scrollY: 0 },
    getComputedStyle() { return { display: 'block', visibility: 'visible', opacity: '1' }; },
    URL,
    URLSearchParams,
    Date,
    console
  };
  const instrumented = source.replace(
    marker,
    '  globalThis.__parserTest = { productRootAndText, extractComposition, extractDescription };\n})();'
  );
  vm.runInNewContext(instrumented, context, { filename: relativePath });
  return context.__parserTest;
}

const productFixture = [
  'Тестовый товар',
  'Описание',
  'Свежий продукт для ежедневного употребления.',
  'Пищевая ценность на 100 г',
  'Белки 1.2 г',
  'Жиры 0.3 г',
  'Состав',
  'яблоко, вода, лимонный сок',
  'Вес',
  '150 г',
  'Вам понравится',
  'Совершенно другой товар',
  'Отзывы покупателей'
].join('\n');

for (const script of scripts) {
  test(script + ': extracts explicit description and composition', () => {
    const parser = loadParser(script, productFixture);
    assert.equal(parser.extractDescription(productFixture), 'Свежий продукт для ежедневного употребления.');
    assert.equal(parser.extractComposition(productFixture), 'яблоко, вода, лимонный сок');
  });

  test(script + ': product text stops before recommendation content', () => {
    const parser = loadParser(script, productFixture);
    const result = parser.productRootAndText();
    assert.match(result.productText, /Свежий продукт/);
    assert.match(result.productText, /яблоко, вода, лимонный сок/);
    assert.doesNotMatch(result.productText, /Совершенно другой товар|Отзывы покупателей/);
  });

  test(script + ': missing description and composition stay null', () => {
    const textWithoutDetails = 'Тестовый товар\nПищевая ценность\nБелки 1.2 г\nВес\n150 г';
    const parser = loadParser(script, textWithoutDetails);
    assert.equal(parser.extractDescription(textWithoutDetails), null);
    assert.equal(parser.extractComposition(textWithoutDetails), null);
  });
}
