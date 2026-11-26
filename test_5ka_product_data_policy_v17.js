const assert = require('node:assert/strict');
const parser = require('./ui_v14/5ka_batch_enrichment_v16.user.js');

assert.equal(parser.classifyProductFromTaxonomy('Каталог / Молочные продукты').product_class, 'food');
assert.equal(parser.classifyProductFromTaxonomy('Каталог / Бытовая химия').product_class, 'non_food');
assert.equal(parser.classifyProductFromTaxonomy('Каталог / Сезонные товары').product_class, 'non_food');
assert.equal(parser.classifyProductFromTaxonomy('').product_class, 'unknown');

const applicability = parser.applicabilityForProductClass('non_food');
assert.equal(applicability.nutrition, 'not_applicable');
assert.equal(applicability.ingredients, 'not_applicable');
assert.equal(applicability.materials, 'applicable');

const nutrition = parser.parseNutrition(
  'Пищевая ценность на 100 г\n14.5 белки\n23.0 жиры\n4.1 углеводы\n284.0 ккал\nСостав\nСыры, масло сливочное'
);
assert.deepEqual(nutrition, {
  Белки: '14.5', Жиры: '23.0', Углеводы: '4.1', Ккал: '284.0'
});

assert.equal(
  parser.extractComposition('Состав\nПолиэтилен, джут.\nВес\n0.37 кг\nБренд\nМаркет'),
  'Полиэтилен, джут.'
);


const realProductText = [
  'Агар Dr. Bakers пищевой 7г',
  '7 г',
  'Цена 134 99 ₽',
  'Пищевая ценность на 100 г',
  '0.4 белки',
  '0.0 жиры',
  '0.0 углеводы',
  '174.0 ккал',
  'Описание',
  'Агар Dr. Bakers Professional обладает оптимальной прочностью геля* и не требует длительного кипячения для его образования.',
  'Идеально подходит для приготовления мармелада, суфле, зефира и других изделий.',
  'Показать полностью',
  'Состав',
  'агар (загуститель, агент желирующий). Может содержать следы молока, глютена, горчицы, сои, яиц, кунжута и орехов',
  'Вес',
  '0.007 кг',
  'Срок хранения',
  '365 дней',
  'Условия хранения',
  'в чистых, хорошо вентилируемых помещениях, без постороннего запаха, резких колебаний температуры воздуха при относительной влажности воздуха не более 80%',
  'Бренд',
  'Dr. Bakers'
].join('\\n');
const productTitle = { innerText: 'Агар Dr. Bakers пищевой 7г' };
const productRoot = {
  innerText: realProductText,
  querySelector(selector) {
    return selector === '[data-qa="product-card-title"]' || selector === 'h1' ? productTitle : null;
  }
};
global.document = {
  querySelector(selector) {
    return selector === '[data-qa="product-screen"]' ? productRoot : null;
  }
};
const scoped = parser.productRootAndText();
assert.equal(scoped.main, productRoot);
assert.equal(scoped.title, productTitle);
assert.equal(scoped.productText, realProductText);
assert.deepEqual(parser.parseNutrition(scoped.productText), {
  Белки: '0.4', Жиры: '0.0', Углеводы: '0.0', Ккал: '174.0'
});
assert.equal(
  parser.extractComposition(scoped.productText),
  'агар (загуститель, агент желирующий). Может содержать следы молока, глютена, горчицы, сои, яиц, кунжута и орехов'
);
const attributes = parser.extractAttributes(scoped.productText, parser.parseNutrition(scoped.productText));
assert.equal(attributes['Вес'], '0.007 кг');
assert.equal(attributes['Срок хранения'], '365 дней');
assert.equal(attributes['Бренд'], 'Dr. Bakers');
assert.equal(attributes['Условия хранения'].startsWith('в чистых, хорошо вентилируемых помещениях'), true);
// The captured page has no category breadcrumb beyond "Каталог"; do not
// infer food classification from the product title.
assert.equal(parser.classifyProductFromTaxonomy('Главная / Каталог / Агар Dr. Bakers пищевой 7г').product_class, 'unknown');

console.log('Pyaterochka V17 taxonomy and DOM parser tests passed.');
