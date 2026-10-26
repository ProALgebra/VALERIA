import { TaskRun } from '../../shared/tasks/task.model';
import { durationText, minutesText, pluralTasks, runStages } from './run-view';

const run = (over: Partial<TaskRun>): TaskRun => ({
    id: 'r-1',
    task_id: 't-ci',
    plan_first: false,
    phase: null,
    title: 'Разбор упавшего CI-пайплайна',
    status: 'running',
    user_input: null,
    error_code: null,
    created_at: '2026-10-24T11:00:00Z',
    started_at: '2026-10-24T11:02:00Z',
    finished_at: null,
    ...over,
});

const states = (r: TaskRun) => runStages(r).map((s) => s.state);

describe('runStages', () => {
    it('marks stages by status', () => {
        expect(states(run({ status: 'queued', started_at: null }))).toEqual(['active', 'future', 'future', 'future']);
        expect(states(run({ status: 'running' }))).toEqual(['done', 'done', 'active', 'future']);
        expect(states(run({ status: 'completed', finished_at: '2026-10-24T11:08:00Z' }))).toEqual([
            'done',
            'done',
            'done',
            'done',
        ]);
        expect(states(run({ status: 'failed' }))).toEqual(['done', 'done', 'error', 'future']);
        expect(states(run({ status: 'cancelled', started_at: null }))).toEqual(['off', 'future', 'future', 'future']);
    });

    it('adds «План» for plan_first and moves on in the build phase', () => {
        const r = run({ plan_first: 1, phase: 'build' });
        expect(runStages(r).map((s) => s.name)).toContain('План');
        expect(states(r)).toEqual(['done', 'done', 'done', 'active', 'future']);
    });
});

describe('run texts', () => {
    it('pluralizes «задание»', () => {
        expect([1, 2, 5, 11, 21, 22].map(pluralTasks)).toEqual([
            'задание',
            'задания',
            'заданий',
            'заданий',
            'задание',
            'задания',
        ]);
    });

    it('formats minutes and duration with queue time', () => {
        expect(minutesText(10_000)).toBe('1 мин');
        expect(minutesText(65 * 60_000)).toBe('1 ч 5 мин');
        expect(durationText(run({ status: 'completed', finished_at: '2026-10-24T11:08:00Z' }))).toBe(
            '8 мин, из них 2 мин в очереди',
        );
    });
});
