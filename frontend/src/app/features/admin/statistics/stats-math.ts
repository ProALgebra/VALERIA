import type { StatRun } from '../config-shared/admin-config.api';
import { dur, durShort, MONTHS_SHORT, plural, zoned } from '../config-shared/format';

/** The source line names this zone; chart labels use it too so they agree with the text. */
export const STATS_TZ = 'Europe/Moscow';

export type RunState = 'done' | 'failed' | 'cancelled' | 'running' | 'queued';

/** One run on the chart. Times in ms, durations in minutes. */
export interface ChartRun {
    id: string;
    kind: 'question' | 'task';
    /** «Вопрос» or the task title. */
    title: string;
    /** Shape key: `q` for questions, otherwise the task id. */
    shapeKey: string;
    /** Login of the user who started the run; null when the API does not send it. */
    who: string | null;
    t: number;
    wait: number;
    work: number;
    st: RunState;
}

const MIN = 60_000;

/** wait = started − queued (or now − queued while waiting); work = finished (or now) − started. */
export function toChartRun(r: StatRun, nowMs: number): ChartRun {
    const created = Date.parse(r.created_at);
    const started = r.started_at ? Date.parse(r.started_at) : null;
    const finished = r.finished_at ? Date.parse(r.finished_at) : null;
    const st: RunState =
        r.outcome === 'completed'
            ? 'done'
            : r.outcome === 'failed'
              ? 'failed'
              : r.outcome === 'cancelled'
                ? 'cancelled'
                : started != null
                  ? 'running'
                  : 'queued';
    const wait = Math.max(0, ((started ?? finished ?? nowMs) - created) / MIN);
    const work = started != null ? Math.max(0, ((finished ?? nowMs) - started) / MIN) : 0;
    return {
        id: r.id,
        kind: r.kind,
        title: r.kind === 'question' ? 'Вопрос' : r.task_title || 'Задача',
        shapeKey: r.kind === 'question' ? 'q' : r.task_id || 'task',
        who: r.login || null,
        t: created,
        wait,
        work,
        st,
    };
}

/** Linear interpolation between closest ranks (prototype `quant`). Empty → 0. */
export function quantile(values: readonly number[], q: number): number {
    if (!values.length) return 0;
    const b = [...values].sort((x, y) => x - y);
    const i = (b.length - 1) * q;
    const lo = Math.floor(i);
    return b[lo] + (b[Math.ceil(i)] - b[lo]) * (i - lo);
}

export interface Kpis {
    count: number;
    /** Over successfully finished runs; null when there are none. */
    medianWork: number | null;
    medianWait: number | null;
    failed: number;
    failedPct: number | null;
}

export function kpis(runs: readonly ChartRun[]): Kpis {
    const fin = runs.filter((r) => r.st === 'done');
    const failed = runs.filter((r) => r.st === 'failed').length;
    return {
        count: runs.length,
        medianWork: fin.length ? quantile(fin.map((r) => r.work), 0.5) : null,
        medianWait: fin.length ? quantile(fin.map((r) => r.wait), 0.5) : null,
        failed,
        failedPct: runs.length ? Math.round((failed / runs.length) * 100) : null,
    };
}

export const niceUp = (v: number, steps: readonly number[]): number => steps.find((s) => s >= v) ?? steps[steps.length - 1];

export interface YScale {
    max: number;
    step: number;
    ticks: number[];
}

/**
 * The axis fits finished runs with their wait and unfinished ones by work only: a run stuck in the queue for hours
 * must not flatten everything else; its stem is clipped at the top of the plot.
 */
export function yScale(runs: readonly ChartRun[]): YScale {
    const top = Math.max(5, ...runs.map((r) => (r.st === 'queued' || r.st === 'running' ? r.work : r.wait + r.work))) * 1.05;
    const max = niceUp(top, [5, 10, 15, 20, 30, 40, 50, 60, 90, 120, 180]);
    const step = niceUp(max / 5, [1, 2, 5, 10, 15, 20, 30, 60]);
    const ticks: number[] = [];
    for (let v = 0; v <= max; v += step) ticks.push(v);
    return { max, step, ticks };
}

/** Minutes the zone is ahead of UTC at `ms`. */
export function zoneOffsetMin(ms: number, timeZone = STATS_TZ): number {
    const z = zoned(ms, timeZone);
    const asUtc = Date.UTC(z.y, z.m - 1, z.d, z.h, z.min);
    return Math.round((asUtc - Math.floor(ms / MIN) * MIN) / MIN);
}

export const hm = (ms: number, timeZone = STATS_TZ): string => {
    const z = zoned(ms, timeZone);
    return `${z.h}:${String(z.min).padStart(2, '0')}`;
};

export const dayLabel = (ms: number, timeZone = STATS_TZ): string => {
    const z = zoned(ms, timeZone);
    return `${z.d} ${MONTHS_SHORT[z.m - 1]}`;
};

/** Position of `ms` on the x axis in %. */
export const xPct = (ms: number, from: number, to: number): number => ((ms - from) / (to - from)) * 100;

/** Time ticks: hours inside a day, days, weeks or months (prototype `controlChart`). */
export function xTicks(from: number, to: number, timeZone = STATS_TZ): { t: number; label: string }[] {
    const spanMin = (to - from) / MIN;
    const step = niceUp(spanMin / 7, [5, 10, 15, 30, 60, 180, 360, 720, 1440, 2880, 10080, 20160, 43200]);
    const out: { t: number; label: string }[] = [];
    if (step >= 43200) {
        const z = zoned(from, timeZone);
        for (let k = 0; k < 40; k++) {
            const guess = Date.UTC(z.y, z.m - 1 + k, 1);
            const t = guess - zoneOffsetMin(guess, timeZone) * MIN;
            if (t > to) break;
            if (t >= from) out.push({ t, label: MONTHS_SHORT[zoned(t, timeZone).m - 1] });
        }
        return out;
    }
    const off = zoneOffsetMin(from, timeZone);
    const localFrom = from / MIN + off;
    for (let local = Math.ceil(localFrom / step) * step; local - off <= to / MIN; local += step) {
        const t = (local - off) * MIN;
        const midnight = ((local % 1440) + 1440) % 1440 === 0;
        out.push({ t, label: step >= 1440 || (midnight && spanMin > 1440) ? dayLabel(t, timeZone) : hm(t, timeZone) });
    }
    return out;
}

/** Histogram bucket size in minutes for a span. */
export function bucketSize(spanMin: number): number {
    if (spanMin <= 60) return 5;
    if (spanMin <= 180) return 15;
    if (spanMin <= 1440) return 60;
    if (spanMin <= 4320) return 180;
    if (spanMin <= 10080) return 360;
    if (spanMin <= 129600) return 1440;
    return 10080;
}

export interface Bucket {
    start: number;
    end: number;
    n: number;
    ok: number;
    failed: number;
    cancelled: number;
}

export function buckets(runs: readonly ChartRun[], from: number, to: number, timeZone = STATS_TZ): Bucket[] {
    const size = bucketSize((to - from) / MIN);
    const off = zoneOffsetMin(from, timeZone);
    const out: Bucket[] = [];
    for (let local = Math.floor((from / MIN + off) / size) * size; (local - off) * MIN < to; local += size) {
        const start = (local - off) * MIN;
        const end = start + size * MIN;
        const inB = runs.filter((r) => r.t >= start && r.t < end);
        const failed = inB.filter((r) => r.st === 'failed').length;
        const cancelled = inB.filter((r) => r.st === 'cancelled').length;
        out.push({ start, end, n: inB.length, ok: inB.length - failed - cancelled, failed, cancelled });
    }
    return out;
}

export function bucketName(b: Bucket, spanMin: number, timeZone = STATS_TZ): string {
    const size = (b.end - b.start) / MIN;
    if (size > 1440) return `${dayLabel(b.start, timeZone)} — ${dayLabel(b.end - 1440 * MIN, timeZone)}`;
    if (size === 1440) return dayLabel(b.start, timeZone);
    return `${spanMin > 1440 ? dayLabel(b.start, timeZone) + ', ' : ''}${hm(b.start, timeZone)}–${hm(b.end, timeZone)}`;
}

export function bucketTooltip(b: Bucket, spanMin: number, timeZone = STATS_TZ): string {
    return `${bucketName(b, spanMin, timeZone)}\n${b.n} ${plural(b.n, 'запуск', 'запуска', 'запусков')}${b.failed ? `, с ошибкой ${b.failed}` : ''}${b.cancelled ? `, отменено ${b.cancelled}` : ''}`;
}

const ST_TEXT: Record<RunState, string> = {
    done: 'успешно',
    failed: 'с ошибкой',
    cancelled: 'отменён',
    running: 'идёт',
    queued: 'ждёт в очереди',
};

export function whenLabel(ms: number, nowMs: number, timeZone = STATS_TZ): string {
    const a = zoned(ms, timeZone);
    const today = zoned(nowMs, timeZone);
    const yest = zoned(nowMs - 1440 * MIN, timeZone);
    const same = (x: typeof a, y: typeof a) => x.y === y.y && x.m === y.m && x.d === y.d;
    const day = same(a, today) ? 'Сегодня' : same(a, yest) ? 'Вчера' : dayLabel(ms, timeZone);
    return `${day}, ${hm(ms, timeZone)}`;
}

/** «Задача · login», or just the title when the owner is unknown. */
const titleWho = (r: ChartRun) => (r.who ? `${r.title} · ${r.who}` : r.title);

/** Point tooltip (prototype: `${task} · ${who}\n${when} · ${st}\n…`). */
export function runTooltip(r: ChartRun, nowMs: number, timeZone = STATS_TZ): string {
    const tail =
        r.st === 'queued' ? `ждёт ${dur(r.wait)}` : `ждал ${dur(r.wait)}, работал ${dur(r.work)}${r.st === 'running' ? ' и продолжает' : ''}`;
    return `${titleWho(r)}\n${whenLabel(r.t, nowMs, timeZone)} · ${ST_TEXT[r.st]}\n${tail}`;
}

/** Labels written on the chart: the longest closed run and the longest wait (≥ 2 min). */
export function chartNotes(runs: readonly ChartRun[]): { run: ChartRun; text: string }[] {
    const closed = runs.filter((r) => r.st === 'done' || r.st === 'failed');
    let longest: ChartRun | null = null;
    let waited: ChartRun | null = null;
    for (const r of closed) {
        if (!longest || r.wait + r.work > longest.wait + longest.work) longest = r;
        if (!waited || r.wait > waited.wait) waited = r;
    }
    const notes: { run: ChartRun; text: string }[] = [];
    if (longest) {
        const total = longest.wait + longest.work;
        notes.push({
            run: longest,
            text: `${titleWho(longest)} · ${durShort(total)}${longest.wait >= 1 ? `, из них ждал ${durShort(longest.wait)}` : ''}`,
        });
    }
    if (waited && waited !== longest && waited.wait >= 2) {
        notes.push({ run: waited, text: `Ждал в очереди ${durShort(waited.wait)} · ${waited.title}` });
    }
    return notes;
}

/** Median and 90th percentile of agent work over successful runs. */
export function workLines(runs: readonly ChartRun[]): { median: number; p90: number } {
    const fin = runs.filter((r) => r.st === 'done').map((r) => r.work);
    return { median: quantile(fin, 0.5), p90: quantile(fin, 0.9) };
}

// ---------------------------------------------------------------- period
export interface PeriodPreset {
    key: string;
    name: string;
    start: { hours?: number; days?: number; years?: number };
    minutes: number;
}

/** The standard periods (prototype `PERIODS`, Koobiq Datetime pipe list). */
export const PERIODS: PeriodPreset[] = [
    { key: '1h', name: 'Последний час', start: { hours: -1 }, minutes: 60 },
    { key: '3h', name: 'Последние 3 часа', start: { hours: -3 }, minutes: 180 },
    { key: '24h', name: 'Последние 24 часа', start: { hours: -24 }, minutes: 1440 },
    { key: '3d', name: 'Последние 3 дня', start: { days: -3 }, minutes: 4320 },
    { key: '7d', name: 'Последние 7 дней', start: { days: -7 }, minutes: 10080 },
    { key: '30d', name: 'Последние 30 дней', start: { days: -30 }, minutes: 43200 },
    { key: '90d', name: 'Последние 90 дней', start: { days: -90 }, minutes: 129600 },
    { key: '1y', name: 'Последний год', start: { years: -1 }, minutes: 525600 },
];
export const DEFAULT_PERIOD = '7d';

export type Period = { preset: PeriodPreset } | { from: string; to: string };

/** `period` query param: a preset key or `<startISO>..<endISO>`. Unknown → default preset. */
export function parsePeriod(param: string | null | undefined): Period {
    const preset = PERIODS.find((p) => p.key === param);
    if (preset) return { preset };
    if (param && param.includes('..')) {
        const [from, to] = param.split('..');
        if (!Number.isNaN(Date.parse(from)) && !Number.isNaN(Date.parse(to)) && Date.parse(from) < Date.parse(to)) return { from, to };
    }
    return { preset: PERIODS.find((p) => p.key === DEFAULT_PERIOD)! };
}

export function periodParam(p: Period): string {
    return 'preset' in p ? p.preset.key : `${p.from}..${p.to}`;
}

/** Absolute range of a period, in ms. */
export function periodRange(p: Period, nowMs: number): { from: number; to: number } {
    if ('preset' in p) return { from: nowMs - p.preset.minutes * MIN, to: nowMs };
    return { from: Date.parse(p.from), to: Date.parse(p.to) };
}

/** «Тип» values from `?type=a,b`: `question`, a task id, or `task` (any task, older links). */
export function parseTypes(param: string | null | undefined): string[] {
    return [...new Set((param ?? '').split(',').map((s) => s.trim()).filter(Boolean))];
}

/** `type` for `GET /api/admin/statistics/runs`: the API takes one value, several are filtered by {@link runsByTypes}. */
export function typeParam(types: readonly string[]): string | null {
    return types.length === 1 ? types[0] : null;
}

/** The client-side part of the «Тип» filter: runs matching any of several types; empty means all. */
export function runsByTypes(items: StatRun[], types: readonly string[]): StatRun[] {
    if (types.length < 2) return items;
    return items.filter((r) =>
        r.kind === 'question' ? types.includes('question') : types.includes('task') || types.includes(r.task_id ?? ''),
    );
}
