/** Formatting shared by the admin screens «Пользователи», «Запросы пользователей», «Аудит». */

/** Empty table cell (SPEC §7). */
export const DASH = '—';

const MONTHS = ['янв', 'фев', 'мар', 'апр', 'мая', 'июн', 'июл', 'авг', 'сен', 'окт', 'ноя', 'дек'];

/**
 * Parses a backend timestamp. The backend sends ISO strings with up to 6 fraction digits
 * (`2026-10-24T14:21:00.000000+00:00`); the fraction is cut to milliseconds before `Date` sees it.
 */
export function parseTime(value: string | null | undefined): Date | null {
    if (!value) return null;
    const ms = Date.parse(value.replace(/(\.\d{3})\d+/, '$1'));
    return Number.isNaN(ms) ? null : new Date(ms);
}

function sameDay(a: Date, b: Date): boolean {
    return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
}

function hhmm(d: Date): string {
    return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

export interface WhenOptions {
    /** Keep «, 09:31» for dates before yesterday (audit). Default false: «21 окт». */
    timeForOld?: boolean;
    now?: Date;
}

/**
 * Prototype dates: «Сегодня, 14:21», «Вчера, 18:02», «21 окт» (or «21 окт, 09:31» with `timeForOld`).
 * Another year adds it: «15 сен 2025». Empty or broken value → «—».
 */
export function formatWhen(value: string | null | undefined, options: WhenOptions = {}): string {
    const d = parseTime(value);
    if (!d) return DASH;
    const now = options.now ?? new Date();
    const yesterday = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1);
    if (sameDay(d, now)) return `Сегодня, ${hhmm(d)}`;
    if (sameDay(d, yesterday)) return `Вчера, ${hhmm(d)}`;
    const year = d.getFullYear() === now.getFullYear() ? '' : ` ${d.getFullYear()}`;
    const day = `${d.getDate()} ${MONTHS[d.getMonth()]}${year}`;
    return options.timeForOld ? `${day}, ${hhmm(d)}` : day;
}

/** Date only: «2 сен» (another year: «2 сен 2025»). */
export function formatDay(value: string | null | undefined, now = new Date()): string {
    const d = parseTime(value);
    if (!d) return DASH;
    const year = d.getFullYear() === now.getFullYear() ? '' : ` ${d.getFullYear()}`;
    return `${d.getDate()} ${MONTHS[d.getMonth()]}${year}`;
}

/** Russian plural: 1 пользователь, 2 пользователя, 5 пользователей (prototype `plural`). */
export function plural(n: number, one: string, few: string, many: string): string {
    const mod10 = n % 10;
    const mod100 = n % 100;
    if (mod10 === 1 && mod100 !== 11) return one;
    if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return few;
    return many;
}

/** «—» for empty values. */
export function orDash(value: string | number | null | undefined): string {
    return value === null || value === undefined || value === '' ? DASH : String(value);
}
