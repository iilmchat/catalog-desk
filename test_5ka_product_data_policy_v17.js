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
  'Полиэтилен, джут'
);

console.log('Pyaterochka V17 taxonomy and DOM parser tests passed.');
