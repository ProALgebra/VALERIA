/**
 * Period filter shared by «Запросы пользователей» and «Аудит».
 * In the URL a preset is a code (`period=7d`), a custom range is `period=custom&from=ISO&to=ISO`.
 * In `kbq-filter-bar` a preset is `{ id, name, start: <duration>, end: null }`, a custom range `{ start: ISO, end: ISO }`.
 */

export interface PeriodPreset {
    id: string;
    name: string;
    /** Luxon-style duration relative to now (negative), used by the Koobiq datetime pipe. */
    start: Record<string, number>;
    end: null;
    /** Window length in milliseconds. */
    ms: number;
}

const HOUR = 3_600_000;
const DAY = 24 * HOUR;

export const PERIODS: readonly PeriodPreset[] = [
    { id: '1h', name: 'Последний час', start: { hours: -1 }, end: null, ms: HOUR },
    { id: '3h', name: 'Последние 3 часа', start: { hours: -3 }, end: null, ms: 3 * HOUR },
    { id: '24h', name: 'Последние 24 часа', start: { hours: -24 }, end: null, ms: DAY },
    { id: '3d', name: 'Последние 3 дня', start: { days: -3 }, end: null, ms: 3 * DAY },
    { id: '7d', name: 'Последние 7 дней', start: { days: -7 }, end: null, ms: 7 * DAY },
    { id: '30d', name: 'Последние 30 дней', start: { days: -30 }, end: null, ms: 30 * DAY },
    { id: '90d', name: 'Последние 90 дней', start: { days: -90 }, end: null, ms: 90 * DAY },
    { id: '1y', name: 'Последний год', start: { years: -1 }, end: null, ms: 365 * DAY },
];

/** Selected period: none, a preset code, or a custom ISO range. */
export type PeriodState = null | { code: string } | { from: string; to: string };

/** Reads `period`/`from`/`to` query params. Unknown codes and broken ranges are ignored. */
export function periodFromQuery(
    period: string | null | undefined,
    from?: string | null,
    to?: string | null,
): PeriodState {
    if (!period) return null;
    if (period === 'custom') {
        if (from && to && !Number.isNaN(Date.parse(from)) && !Number.isNaN(Date.parse(to))) return { from, to };
        return null;
    }
    return PERIODS.some((p) => p.id === period) ? { code: period } : null;
}

/** Query params for a period (keys with null are removed from the URL by the router). */
export function periodToQuery(state: PeriodState): { period: string | null; from: string | null; to: string | null } {
    if (!state) return { period: null, from: null, to: null };
    if ('code' in state) return { period: state.code, from: null, to: null };
    return { period: 'custom', from: state.from, to: state.to };
}

/** Value for the Koobiq datetime pipe. */
export function periodToPipe(state: PeriodState): unknown {
    if (!state) return null;
    if ('code' in state) return PERIODS.find((p) => p.id === state.code) ?? null;
    return { start: state.from, end: state.to };
}

/** Reads the value the datetime pipe wrote. */
export function periodFromPipe(value: unknown): PeriodState {
    if (!value || typeof value !== 'object') return null;
    const v = value as { id?: unknown; name?: unknown; start?: unknown; end?: unknown };
    const preset = PERIODS.find((p) => p.id === v.id || p.name === v.name);
    if (preset) return { code: preset.id };
    if (typeof v.start === 'string' && typeof v.end === 'string') return { from: v.start, to: v.end };
    return null;
}

/** API `from`/`to` (ISO, UTC) for the period, counted from `now`. */
export function periodRange(state: PeriodState, now = new Date()): { from: string | null; to: string | null } {
    if (!state) return { from: null, to: null };
    if ('code' in state) {
        const preset = PERIODS.find((p) => p.id === state.code);
        return preset
            ? { from: new Date(now.getTime() - preset.ms).toISOString(), to: null }
            : { from: null, to: null };
    }
    return { from: new Date(state.from).toISOString(), to: new Date(state.to).toISOString() };
}

/** Splits a comma list query param: `a,b` → `['a', 'b']`. */
export function listFromQuery(value: string | null | undefined): string[] {
    return (value ?? '')
        .split(',')
        .map((s) => s.trim())
        .filter(Boolean);
}

/** Joins a list for a query param; empty → null (param removed). */
export function listToQuery(values: readonly string[]): string | null {
    return values.length ? values.join(',') : null;
}
