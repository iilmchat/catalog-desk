const assert = require('node:assert/strict');
const parser = require('./ui_v14/pr_batch_enrichment_v16.user.js');

const title = { innerText: 'Ананас клубника голубика свежие очищенные нарезанные Зелёная Линия, 150г' };
const productRoot = {
  innerText: [
    title.innerText,
    'Пищевая ценность на 100 г',
    'Пищевая ценность на 100 г 52.7 ккал 1.2 белки 0.3 жиры 11.2 углеводы',
    'Вес 0.15 кг',
    'Бренд Зелёная Линия',
    'Страна производства Россия'
  ].join('\n'),
  querySelector(selector) {
    if (selector === '[data-qa="product-card-title"]') return title;
    if (selector === '[data-qa="product-description"]') return null;
    return null;
  },
  querySelectorAll(selector) {
    if (selector !== '[data-qa="product-detail-attribute"]') return [];
    return [
      [['Вес'], ['0.15 кг']],
      [['Бренд'], ['Зелёная Линия']],
      [['Страна производства'], ['Россия']]
    ].map(parts => ({
      querySelectorAll: () => parts.map(([text]) => ({ innerText: text }))
    }));
  }
};
global.document = {
  querySelector(selector) {
    return selector === '[data-qa="product-screen"]' ? productRoot : null;
  }
};

const scoped = parser.productRootAndText();
assert.equal(scoped.main, productRoot);
assert.equal(scoped.title, title);
assert.ok(!scoped.productText.includes('Вам понравится'));

const nutrition = parser.parseNutrition(productRoot.innerText);
assert.deepEqual(nutrition, {
  Белки: '1.2', Жиры: '0.3', Углеводы: '11.2', Ккал: '52.7'
});
const attrs = parser.extractAttributesFromDom(productRoot, nutrition);
assert.equal(attrs['Вес'], '0.15 кг');
assert.equal(attrs['Бренд'], 'Зелёная Линия');
assert.equal(attrs['Страна производства'], 'Россия');
assert.equal(attrs['Белки'], '1.2 г');
assert.equal(parser.extractComposition(scoped.productText), null);
assert.equal(
  parser.extractComposition('Состав\nДревесина, джут.\nВес\n0.37 кг\nБренд\nМаркет'),
  'Древесина, джут.'
);
assert.equal(parser.extractDescription(productRoot, title.innerText), null);


assert.equal(parser.classifyProductFromTaxonomy('Каталог / Фрукты и овощные нарезки').product_class, 'food');
assert.equal(parser.classifyProductFromTaxonomy('Каталог / Новогодние товары').product_class, 'non_food');
assert.equal(parser.classifyProductFromTaxonomy('').product_class, 'unknown');
assert.equal(parser.applicabilityForProductClass('non_food').nutrition, 'not_applicable');
assert.equal(parser.applicabilityForProductClass('non_food').materials, 'applicable');
assert.equal(parser.applicabilityForProductClass('food').nutrition, 'applicable');

console.log('Perekrestok DOM parser tests passed.');
