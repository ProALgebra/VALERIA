import type { QueueOverview, QueueRow } from '../config-shared/admin-config.api';
import { NAME_TAKEN } from '../config-shared/names';
import { archiveBlock, fromOverview, planRequests, QueueFormValue, unfinished, usedBy, validateQueues, visibleRows } from './queue-form';

const q = (id: string, name: string, priority: number, max: number, extra: Partial<QueueRow> = {}): QueueRow => ({
    id,
    name,
    priority,
    max_workers: max,
    status: 'active',
    is_system: 0,
    created_at: '2026-09-01T00:00:00Z',
    updated_at: `2026-09-01T00:00:00.00000${id.length}+00:00`,
    active: 0,
    queued: 0,
    ...extra,
});

const overview: QueueOverview = {
    limit: 4,
    env_limit: 2,
    hard_limit: 10,
    active: 3,
    queued: 2,
    default_chat_queue_id: 'default',
    queues: [q('questions', 'Вопросы', 200, 1), q('tasks', 'Задачи', 100, 2, { active: 2, queued: 2 }), q('default', 'Основная', 0, 4, { is_system: 1 }), q('long', 'Долгие', -10, 1)],
    jobs: [],
};
const consult = { model: 'anthropic/claude-sonnet-4-5', queue_id: 'questions' };
const initial = () => fromOverview(overview, consult);
const edit = (fn: (v: QueueFormValue) => void) => {
    const v = structuredClone(initial());
    fn(v);
    return v;
};

describe('queue form payload', () => {
    it('reads an override and plans nothing when unchanged', () => {
        expect(initial().useEnv).toBe(false);
        expect(planRequests(initial(), initial())).toEqual([]);
    });

    it('«Вернуть N из конфигурации» deletes the override', () => {
        expect(planRequests(initial(), edit((v) => (v.useEnv = true)))).toEqual([{ kind: 'concurrency-reset' }]);
    });

    it('a new limit is a PATCH with the value', () => {
        expect(planRequests(initial(), edit((v) => (v.limit = 6)))).toEqual([{ kind: 'concurrency', value: 6 }]);
    });

    it('changed queues echo updated_at, new ones are POSTed, routing goes before archiving', () => {
        const v = edit((v) => {
            v.rows[1].max_workers = 3;
            v.rows.push({ id: null, key: 'new-1', name: ' Ночные ', priority: null, max_workers: 1, is_system: false, updated_at: null, active: 0, queued: 0 });
            v.archived.push('long');
            v.consultQueue = 'tasks';
            v.chatQueue = 'questions';
        });
        expect(planRequests(initial(), v)).toEqual([
            { kind: 'patch', id: 'tasks', body: { name: 'Задачи', priority: 100, max_workers: 3, updated_at: overview.queues[1].updated_at } },
            { kind: 'create', key: 'new-1', body: { name: 'Ночные', priority: 0, max_workers: 1 } },
            { kind: 'consultations', body: { model: 'anthropic/claude-sonnet-4-5', queue_id: 'tasks' } },
            { kind: 'chat-queue', queue_id: 'questions' },
            { kind: 'archive', id: 'long' },
        ]);
        expect(visibleRows(v).map((r) => r.key)).not.toContain('long');
    });

    it('validates limit, names and worker counts', () => {
        const v = edit((v) => {
            v.limit = 11;
            v.rows[3].name = 'вопросы';
            v.rows[2].max_workers = 0;
        });
        expect(validateQueues(v, 10)).toEqual({ limit: 'От 1 до 10', 'long:name': NAME_TAKEN, 'questions:name': NAME_TAKEN, 'default:max_workers': 'От 1 до 10' });
        expect(validateQueues(edit((v) => (v.useEnv = true)), 10)).toEqual({});
    });

    it('explains why a busy queue cannot be archived', () => {
        const ctx = { consultQueue: 'questions', chatQueue: 'default', taskQueues: ['tasks', 'tasks', 'default'] };
        expect(usedBy('default', ctx)).toBe('работа в папке, 1 задача');
        expect(usedBy('tasks', ctx)).toBe('2 задачи');
        const tasks = initial().rows[1];
        expect(archiveBlock(tasks, usedBy('tasks', ctx))).toEqual({
            title: 'Очередь «Задачи» нельзя архивировать',
            text: 'Её используют 2 задачи, и в ней 4 незавершённых задания. Перенесите задачи в другую очередь в их настройках и дождитесь окончания заданий.',
        });
        expect(archiveBlock(initial().rows[3], usedBy('long', ctx))).toBeNull();
        expect(unfinished(1)).toBe('одно незавершённое задание');
        expect(unfinished(5)).toBe('5 незавершённых заданий');
    });
});
