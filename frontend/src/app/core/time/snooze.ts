/** Snooze options of the chat menu (prototype `SNOOZE`). All times are in the browser's local time. */
export type SnoozeOption = '1h' | 'tomorrow' | 'monday';

export const SNOOZE_OPTIONS: readonly { id: SnoozeOption; label: string }[] = [
    { id: '1h', label: 'На 1 час' },
    { id: 'tomorrow', label: 'До 10 утра завтра' },
    { id: 'monday', label: 'До 10 утра понедельника' },
];

/** Moment of return for an option. «Понедельник» is the next Monday (a week ahead if today is Monday). */
export function snoozeUntil(option: SnoozeOption, now: Date = new Date()): Date {
    if (option === '1h') return new Date(now.getTime() + 60 * 60 * 1000);
    const d = new Date(now.getFullYear(), now.getMonth(), now.getDate(), 10, 0, 0, 0);
    if (option === 'tomorrow') {
        d.setDate(d.getDate() + 1);
        return d;
    }
    const day = now.getDay(); // 0 = Sunday
    const ahead = ((8 - day) % 7) || 7;
    d.setDate(d.getDate() + ahead);
    return d;
}

/** ISO 8601 with the local offset, e.g. `2026-10-26T10:00:00+03:00` (required by `/snooze`). */
export function toIsoWithOffset(date: Date): string {
    const pad = (n: number) => String(Math.trunc(Math.abs(n))).padStart(2, '0');
    const offset = -date.getTimezoneOffset();
    const sign = offset >= 0 ? '+' : '-';
    return (
        `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}` +
        `T${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}` +
        `${sign}${pad(offset / 60)}:${pad(offset % 60)}`
    );
}

const WEEKDAY_SHORT = ['вс', 'пн', 'вт', 'ср', 'чт', 'пт', 'сб'];
const WEEKDAY_ACCUSATIVE = [
    'в воскресенье',
    'в понедельник',
    'во вторник',
    'в среду',
    'в четверг',
    'в пятницу',
    'в субботу',
];

function hhmm(d: Date): string {
    return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

function dayDiff(a: Date, b: Date): number {
    const da = new Date(a.getFullYear(), a.getMonth(), a.getDate()).getTime();
    const db = new Date(b.getFullYear(), b.getMonth(), b.getDate()).getTime();
    return Math.round((db - da) / 86_400_000);
}

/** Return time in the menu: «15:30» today, «вс, 10:00» within a week, «02.11, 10:00» later. */
export function snoozeNavLabel(until: Date, now: Date = new Date()): string {
    const diff = dayDiff(now, until);
    if (diff === 0) return hhmm(until);
    if (diff > 0 && diff < 7) return `${WEEKDAY_SHORT[until.getDay()]}, ${hhmm(until)}`;
    const dd = String(until.getDate()).padStart(2, '0');
    const mm = String(until.getMonth() + 1).padStart(2, '0');
    return `${dd}.${mm}, ${hhmm(until)}`;
}

/** «в 15:30» / «завтра в 10:00» / «в понедельник в 10:00» for the «Чат отложен» toast. */
export function snoozeWhenText(until: Date, now: Date = new Date()): string {
    const diff = dayDiff(now, until);
    if (diff === 0) return `в ${hhmm(until)}`;
    if (diff === 1) return `завтра в ${hhmm(until)}`;
    if (diff > 1 && diff < 7) return `${WEEKDAY_ACCUSATIVE[until.getDay()]} в ${hhmm(until)}`;
    const dd = String(until.getDate()).padStart(2, '0');
    const mm = String(until.getMonth() + 1).padStart(2, '0');
    return `${dd}.${mm} в ${hhmm(until)}`;
}
