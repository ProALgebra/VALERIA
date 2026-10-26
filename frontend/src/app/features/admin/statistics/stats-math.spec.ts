import type { StatRun } from '../config-shared/admin-config.api';
import {
    bucketSize,
    buckets,
    bucketTooltip,
    chartNotes,
    ChartRun,
    kpis,
    parsePeriod,
    periodParam,
    periodRange,
    quantile,
    runTooltip,
    toChartRun,
    workLines,
    xTicks,
    yScale,
} from './stats-math';

const iso = (s: string) => `2026-10-24T${s}:00.000000+00:00`;
const NOW = Date.parse('2026-10-24T11:30:00Z'); // 14:30 Moscow

function run(over: Partial<StatRun> = {}): StatRun {
    return {
        id: 's-1',
        kind: 'task',
        task_id: 't-ci',
        task_title: 'Разбор упавшего CI-пайплайна',
        created_at: iso('08:00'),
        started_at: iso('08:02'),
        finished_at: iso('08:12'),
        outcome: 'completed',
        login: null,
        ...over,
    };
}

const cr = (wait: number, work: number, st: ChartRun['st'] = 'done', t = NOW): ChartRun => ({
    id: `${wait}-${work}-${st}`,
    kind: 'task',
    title: 'Ревью merge request',
    shapeKey: 't-mr',
    who: null,
    t,
    wait,
    work,
    st,
});

describe('toChartRun', () => {
    it('splits a finished run into queue wait and agent work', () => {
        const r = toChartRun(run(), NOW);
        expect(r.wait).toBe(2);
        expect(r.work).toBe(10);
        expect(r.st).toBe('done');
        expect(r.shapeKey).toBe('t-ci');
    });

    it('measures a running run up to now', () => {
        const r = toChartRun(run({ started_at: iso('11:20'), created_at: iso('11:15'), finished_at: null, outcome: null }), NOW);
        expect(r.st).toBe('running');
        expect(r.wait).toBe(5);
        expect(r.work).toBe(10);
    });

    it('a queued run only waits', () => {
        const r = toChartRun(run({ created_at: iso('11:28'), started_at: null, finished_at: null, outcome: null }), NOW);
        expect(r.st).toBe('queued');
        expect(r.wait).toBe(2);
        expect(r.work).toBe(0);
    });

    it('questions use the «?» shape and the «Вопрос» title', () => {
        const r = toChartRun(run({ kind: 'question', task_id: null, task_title: null }), NOW);
        expect(r.shapeKey).toBe('q');
        expect(r.title).toBe('Вопрос');
    });

    it('maps failed and cancelled outcomes', () => {
        expect(toChartRun(run({ outcome: 'failed' }), NOW).st).toBe('failed');
        expect(toChartRun(run({ outcome: 'cancelled' }), NOW).st).toBe('cancelled');
    });
});

describe('quantile', () => {
    it('interpolates linearly between ranks', () => {
        expect(quantile([1, 2, 3, 4], 0.5)).toBe(2.5);
        expect(quantile([10, 1, 5], 0.5)).toBe(5);
        expect(quantile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 0.9)).toBeCloseTo(9.1);
        expect(quantile([], 0.5)).toBe(0);
        expect(quantile([7], 0.9)).toBe(7);
    });
});

describe('kpis', () => {
    it('medians count only successful runs, the failure share counts all', () => {
        const k = kpis([cr(1, 4), cr(3, 8), cr(0, 30, 'failed'), cr(10, 0, 'queued')]);
        expect(k.count).toBe(4);
        expect(k.medianWork).toBe(6);
        expect(k.medianWait).toBe(2);
        expect(k.failed).toBe(1);
        expect(k.failedPct).toBe(25);
    });

    it('is empty-safe', () => {
        expect(kpis([])).toEqual({ count: 0, medianWork: null, medianWait: null, failed: 0, failedPct: null });
    });

    it('work lines are the median and p90 of successful work', () => {
        const lines = workLines([cr(0, 1), cr(0, 2), cr(0, 3), cr(0, 4), cr(0, 100, 'failed')]);
        expect(lines.median).toBe(2.5);
        expect(lines.p90).toBeCloseTo(3.7);
    });
});

describe('scales', () => {
    it('rounds the y axis up to a nice maximum and step', () => {
        expect(yScale([cr(1, 2)])).toEqual({ max: 10, step: 2, ticks: [0, 2, 4, 6, 8, 10] });
        expect(yScale([cr(5, 30)]).max).toBe(40);
        expect(yScale([cr(5, 30)]).step).toBe(10);
    });

    it('labels hours inside a day and days across a week (Moscow time)', () => {
        const day = xTicks(NOW - 1440 * 60_000, NOW);
        expect(day.map((t) => t.label)).toContain('0:00');
        expect(day.some((t) => t.label === '18:00')).toBe(true);
        const week = xTicks(NOW - 7 * 1440 * 60_000, NOW);
        expect(week[0].label).toBe('18 окт');
        expect(week.at(-1)!.label).toBe('24 окт');
    });

    it('chooses bucket sizes by span', () => {
        expect(bucketSize(60)).toBe(5);
        expect(bucketSize(1440)).toBe(60);
        expect(bucketSize(10080)).toBe(360);
        expect(bucketSize(43200)).toBe(1440);
        expect(bucketSize(525600)).toBe(10080);
    });

    it('stacks buckets by outcome and names them', () => {
        const from = Date.parse('2026-10-24T05:00:00Z'); // 8:00 MSK
        const to = Date.parse('2026-10-24T08:00:00Z');
        const list = buckets([cr(0, 1, 'done', from + 60_000), cr(0, 1, 'failed', from + 120_000), cr(0, 1, 'cancelled', from + 16 * 60_000)], from, to);
        expect(list).toHaveLength(12);
        expect(list[0]).toMatchObject({ n: 2, ok: 1, failed: 1, cancelled: 0 });
        expect(bucketTooltip(list[0], 180)).toBe('8:00–8:15\n2 запуска, с ошибкой 1');
        expect(bucketTooltip(list[1], 180)).toBe('8:15–8:30\n1 запуск, отменено 1');
    });
});

describe('texts', () => {
    it('point tooltip', () => {
        const r = toChartRun(run(), NOW);
        expect(runTooltip(r, NOW)).toBe('Разбор упавшего CI-пайплайна\nСегодня, 11:00 · успешно\nждал 2 мин, работал 10 мин');
        const live = toChartRun(run({ started_at: iso('11:20'), created_at: iso('11:20'), finished_at: null, outcome: null }), NOW);
        expect(runTooltip(live, NOW)).toContain('и продолжает');
        expect(runTooltip(toChartRun(run({ login: 'm.belov' }), NOW), NOW).split('\n')[0]).toBe('Разбор упавшего CI-пайплайна · m.belov');
    });

    it('notes the longest run and the longest wait', () => {
        const notes = chartNotes([cr(1.5, 30), cr(6, 2), cr(9, 0, 'queued')]);
        expect(notes.map((n) => n.text)).toEqual(['Ревью merge request · 32 мин, из них ждал 2 мин', 'Ждал в очереди 6 мин · Ревью merge request']);
    });

    it('puts the login into the longest-run note and skips a short or same-run wait', () => {
        const long = { ...cr(3, 40), who: 'm.belov' };
        expect(chartNotes([long, cr(1.5, 2), cr(30, 0, 'cancelled')]).map((n) => n.text)).toEqual([
            'Ревью merge request · m.belov · 43 мин, из них ждал 3 мин',
        ]);
    });
});

describe('period param', () => {
    it('round-trips presets and custom ranges', () => {
        expect(periodParam(parsePeriod('24h'))).toBe('24h');
        expect(periodParam(parsePeriod(null))).toBe('7d');
        expect(periodParam(parsePeriod('nonsense'))).toBe('7d');
        const custom = '2026-10-18T00:00:00.000+03:00..2026-10-18T23:59:00.000+03:00';
        expect(periodParam(parsePeriod(custom))).toBe(custom);
        expect(periodRange(parsePeriod(custom), NOW).from).toBe(Date.parse('2026-10-17T21:00:00Z'));
        expect(periodRange(parsePeriod('3h'), NOW)).toEqual({ from: NOW - 3 * 3600_000, to: NOW });
    });
});
