import type { AdminTask } from '../config-shared/admin-config.api';
import { NAME_TAKEN, NAME_TOO_LONG } from '../config-shared/names';
import { audienceLabel, CONTENT_REQUIRED, emptyTask, fromTask, sameTask, toPayload, validateTask } from './task-form';

const task: AdminTask = {
    id: 't-ci',
    title: 'Разбор упавшего CI-пайплайна',
    description: 'Описание',
    content: 'Инструкция',
    agent: 'build',
    provider_id: 'anthropic',
    model_id: 'claude-sonnet-4-5',
    model_variant: null,
    queue_id: 'tasks',
    is_published: 1,
    visibility: 'selected',
    updated_at: '2026-10-14T09:00:00.123456+00:00',
    plan_first: 0,
    input_hint: null,
    model: 'anthropic/claude-sonnet-4-5',
    audience_user_ids: ['u-pvolkov', 'u-isokolova'],
    next_task_ids: [],
};

const ruPlural = (n: number) => (n === 1 ? 'пользователь' : n < 5 ? 'пользователя' : 'пользователей');

describe('task form', () => {
    it('round-trips a task into a PATCH body with visibility and user_ids together', () => {
        const body = toPayload(fromTask(task), { updated_at: task.updated_at });
        expect(body).toMatchObject({
            title: 'Разбор упавшего CI-пайплайна',
            description: 'Описание',
            input_hint: null,
            model: 'anthropic/claude-sonnet-4-5',
            model_variant: null,
            plan_first: false,
            visibility: 'selected',
            user_ids: ['u-pvolkov', 'u-isokolova'],
            updated_at: '2026-10-14T09:00:00.123456+00:00',
        });
    });

    it('drops the user list when everyone sees the task and trims texts', () => {
        const v = { ...fromTask(task), visibility: 'all' as const, title: '  Новая  ', description: '  ', model_variant: ' high ' };
        const body = toPayload(v);
        expect(body.user_ids).toEqual([]);
        expect(body.title).toBe('Новая');
        expect(body.description).toBeNull();
        expect(body.model_variant).toBe('high');
    });

    it('validates name, instruction and hint', () => {
        const v = { ...emptyTask(null, ['tasks']), title: 'разбор упавшего ci-пайплайна' };
        expect(validateTask(v, [task.title])).toEqual({ title: NAME_TAKEN, content: CONTENT_REQUIRED });
        expect(validateTask({ ...v, title: 'x'.repeat(121), content: 'a' }, []).title).toBe(NAME_TOO_LONG);
        expect(validateTask({ ...v, title: 'ok', content: 'a', input_hint: 'x'.repeat(501) }, []).input_hint).toBeTruthy();
        expect(validateTask({ ...v, title: 'ok', content: 'a' }, [])).toEqual({});
    });

    it('defaults new tasks to the «Задачи» queue and options defaults', () => {
        const v = emptyTask({ agents: ['build', 'plan'], default_agent: 'build', default_model: 'openai/gpt-5-codex', models: [] }, ['default', 'tasks']);
        expect(v.queue_id).toBe('tasks');
        expect(v.model).toBe('openai/gpt-5-codex');
    });

    it('treats whitespace-only edits as clean', () => {
        const v = fromTask(task);
        expect(sameTask(v, { ...v, title: ` ${v.title} ` })).toBe(true);
        expect(sameTask(v, { ...v, plan_first: true })).toBe(false);
    });

    it('labels the audience', () => {
        expect(audienceLabel(task, ruPlural)).toBe('2 пользователя');
        expect(audienceLabel({ ...task, visibility: 'all' }, ruPlural)).toBe('Все');
        expect(audienceLabel({ ...task, is_published: 0 }, ruPlural)).toBe('—');
    });
});
