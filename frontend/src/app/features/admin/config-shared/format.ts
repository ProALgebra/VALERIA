/** Formatters shared by the admin configuration screens (prototype `plural`, `dur`, `durShort`, `fmtDay`). */

export function plural(n: number, one: string, few: string, many: string): string {
    const n10 = n % 10;
    const n100 = n % 100;
    if (n10 === 1 && n100 !== 11) return one;
    if (n10 >= 2 && n10 <= 4 && (n100 < 12 || n100 > 14)) return few;
    return many;
}

/** «1 ч 42 мин», «6 мин 05 с», «40 с». Input in minutes. */
export function dur(min: number): string {
    const s = Math.round(min * 60);
    if (s >= 3600) return `${Math.floor(s / 3600)} ч ${Math.round((s % 3600) / 60)} мин`;
    if (s >= 60) return `${Math.floor(s / 60)} мин${s % 60 ? ' ' + String(s % 60).padStart(2, '0') + ' с' : ''}`;
    return `${s} с`;
}

/** «12 мин», «40 с». Input in minutes. */
export function durShort(min: number): string {
    return min >= 1 ? `${Math.round(min)} мин` : `${Math.round(min * 60)} с`;
}

/** Live duration for the queue table: minutes are enough above an hour, seconds below a minute. */
export function durLive(ms: number): string {
    const s = Math.max(0, Math.round(ms / 1000));
    if (s >= 3600) return `${Math.floor(s / 3600)} ч ${Math.floor((s % 3600) / 60)} мин`;
    if (s >= 60) return `${Math.floor(s / 60)} мин`;
    return `${s} с`;
}

export const MONTHS_SHORT = ['янв', 'фев', 'мар', 'апр', 'мая', 'июн', 'июл', 'авг', 'сен', 'окт', 'ноя', 'дек'];

/** Calendar parts of an instant in a time zone (`undefined` = browser zone). */
export function zoned(ms: number, timeZone?: string): { y: number; m: number; d: number; h: number; min: number } {
    const parts = Object.fromEntries(
        new Intl.DateTimeFormat('en-US', {
            timeZone,
            hourCycle: 'h23',
            year: 'numeric',
            month: 'numeric',
            day: 'numeric',
            hour: 'numeric',
            minute: 'numeric',
        })
            .formatToParts(new Date(ms))
            .map((p) => [p.type, p.value]),
    );
    return { y: +parts['year'], m: +parts['month'], d: +parts['day'], h: +parts['hour'] % 24, min: +parts['minute'] };
}

/** «14 окт», or «14 окт 2025» for another year. */
export function dayMonth(ms: number, nowMs: number, timeZone?: string): string {
    const a = zoned(ms, timeZone);
    const b = zoned(nowMs, timeZone);
    return `${a.d} ${MONTHS_SHORT[a.m - 1]}${a.y !== b.y ? ' ' + a.y : ''}`;
}

/** «Сегодня» / «Вчера» / «14 окт» — the «Изменена» column. */
export function changedLabel(iso: string, nowMs = Date.now()): string {
    const ms = Date.parse(iso);
    if (Number.isNaN(ms)) return '—';
    const a = zoned(ms);
    const b = zoned(nowMs);
    const y = zoned(nowMs - 86_400_000);
    if (a.y === b.y && a.m === b.m && a.d === b.d) return 'Сегодня';
    if (a.y === y.y && a.m === y.m && a.d === y.d) return 'Вчера';
    return dayMonth(ms, nowMs);
}

export const EMPTY = '—';

/** For strings that end up in `innerHTML` (the modal renders `kbqContent` and footer labels as HTML). */
export function escapeHtml(s: string): string {
    return s.replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
}
