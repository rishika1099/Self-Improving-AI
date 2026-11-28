const nonLatinLetter = /(?=\p{L})[^\p{Script=Latin}]/u;

export function englishText(value, fallback = 'Original-language text retained in the source record.') {
  return typeof value === 'string' && nonLatinLetter.test(value) ? fallback : value;
}

export function englishJson(value) {
  return JSON.stringify(value, (_key, item) => englishText(item), 2);
}
