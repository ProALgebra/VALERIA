import { matches, normalizeForSearch, tokenize } from './search';

describe('search', () => {
    it('trims the query but keeps inner spaces inside quotes', () => {
        expect(tokenize('   pinot   toscana  ')).toEqual(['pinot', 'toscana']);
        expect(tokenize('"pinot  toscana"')).toEqual(['pinot  toscana']);
    });

    it('treats a quoted phrase as one token and mixes with plain tokens', () => {
        expect(tokenize('red "pinot toscana"')).toEqual(['red', 'pinot toscana']);
    });

    it('ignores an unclosed quote and empty quotes', () => {
        expect(tokenize('"pinot toscana')).toEqual(['pinot toscana']);
        expect(tokenize('"" red')).toEqual(['red']);
    });

    it('returns no tokens for an empty or blank query, and matches everything then', () => {
        expect(tokenize('')).toEqual([]);
        expect(tokenize('   ')).toEqual([]);
        expect(matches(['anything'], '  ')).toBe(true);
    });

    it('is case-insensitive, including Cyrillic', () => {
        expect(matches(['Ошибка в Сборке'], 'сборке')).toBe(true);
        expect(matches(['deploy'], 'DEPLOY')).toBe(true);
    });

    it('searches from the first character', () => {
        expect(matches(['Валерия'], 'в')).toBe(true);
    });

    it('uses AND logic across tokens and OR across fields', () => {
        const fields = ['Pinot noir', 'Toscana'];
        expect(matches(fields, 'pinot toscana')).toBe(true);
        expect(matches(fields, 'pinot bordeaux')).toBe(false);
        expect(matches(fields, '"pinot toscana"')).toBe(false);
        expect(matches(['Pinot Toscana'], '"pinot toscana"')).toBe(true);
    });

    it('ignores diacritics both ways', () => {
        expect(matches(['Château Margaux'], 'chateau')).toBe(true);
        expect(matches(['Chateau Margaux'], 'château')).toBe(true);
        expect(normalizeForSearch('Crème Brûlée')).toBe('creme brulee');
    });

    it('keeps й and ё searchable by their base letters', () => {
        expect(matches(['Тёмная тема'], 'темная')).toBe(true);
        expect(matches(['Мой отчёт'], 'мои')).toBe(true);
    });

    it('skips empty fields and accepts numbers', () => {
        expect(matches([null, undefined, '', 42], '42')).toBe(true);
    });

    it('accepts pre-tokenized input', () => {
        expect(matches(['alpha beta'], tokenize('beta alpha'))).toBe(true);
    });
});
