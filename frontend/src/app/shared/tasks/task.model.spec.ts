import { findActiveDuplicate, TaskRun } from './task.model';

const run = (over: Partial<TaskRun>): TaskRun => ({
    id: 'r-1',
    task_id: 't-mr',
    plan_first: false,
    phase: null,
    title: 'Ревью merge request',
    status: 'running',
    user_input: 'https://gitlab.example.com/mr/1932',
    error_code: null,
    created_at: '2026-10-24T10:58:00Z',
    started_at: '2026-10-24T10:59:00Z',
    finished_at: null,
    ...over,
});

describe('findActiveDuplicate', () => {
    it('finds an unfinished run of the same task with the same trimmed addition', () => {
        const runs = [run({})];
        expect(findActiveDuplicate(runs, 't-mr', '  https://gitlab.example.com/mr/1932 \n')?.id).toBe('r-1');
    });

    it('ignores other tasks, other additions and finished runs', () => {
        const runs = [
            run({ id: 'a', task_id: 't-ci' }),
            run({ id: 'b', user_input: 'https://gitlab.example.com/mr/1933' }),
            run({ id: 'c', status: 'completed' }),
            run({ id: 'd', status: 'failed' }),
            run({ id: 'e', status: 'cancelled' }),
        ];
        expect(findActiveDuplicate(runs, 't-mr', 'https://gitlab.example.com/mr/1932')).toBeNull();
    });

    it('treats queued and waiting runs as active and returns the newest', () => {
        const runs = [
            run({ id: 'old', status: 'queued', created_at: '2026-10-24T09:00:00Z' }),
            run({ id: 'new', status: 'waiting_question', created_at: '2026-10-24T11:00:00Z' }),
        ];
        expect(findActiveDuplicate(runs, 't-mr', 'https://gitlab.example.com/mr/1932')?.id).toBe('new');
    });

    it('matches an empty addition with a null one and compares ids as strings', () => {
        const runs = [run({ task_id: 7, user_input: null })];
        expect(findActiveDuplicate(runs, '7', '   ')?.id).toBe('r-1');
    });
});
