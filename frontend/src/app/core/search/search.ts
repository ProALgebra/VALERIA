/**
 * Client-side search rules (SPEC §12):
 * - the query is trimmed, inner spaces are kept;
 * - split by whitespace into tokens, a "quoted phrase" is one token;
 * - every token must match at least one field (AND);
 * - case- and diacritics-insensitive: NFKD, strip U+0300–U+036F, toLocaleLowerCase.
 * Search starts from the first character; an empty query matches everything.
 */

export function normalizeForSearch(value: string): string {
    return value
        .normalize('NFKD')
        .replace(/[\u0300-\u036f]/g, '')
        .toLocaleLowerCase();
}

/** Splits the query into normalized tokens. */
export function tokenize(query: string): string[] {
    const text = query.trim();
    const tokens: string[] = [];
    const re = /"([^"]*)"?|(\S+)/g;
    let m: RegExpExecArray | null;
    while ((m = re.exec(text)) !== null) {
        const raw = m[1] !== undefined ? m[1] : m[2];
        const token = normalizeForSearch(raw ?? '');
        if (token.trim()) tokens.push(token);
    }
    return tokens;
}

type Field = string | number | null | undefined;

/**
 * True when every token is found in at least one of the fields.
 * `tokens` can be a raw query string or the result of `tokenize` (cheaper in a loop).
 */
export function matches(fields: readonly Field[], tokens: string | readonly string[]): boolean {
    const list = typeof tokens === 'string' ? tokenize(tokens) : tokens;
    if (!list.length) return true;
    const haystack = fields.filter((f) => f !== null && f !== undefined && f !== '').map((f) => normalizeForSearch(String(f)));
    return list.every((token) => haystack.some((field) => field.includes(token)));
}
