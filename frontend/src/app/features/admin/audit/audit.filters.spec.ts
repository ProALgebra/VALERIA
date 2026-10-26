import { REQUEST_NO_FILTERS, requestFilterParams, requestsByTasks } from '../requests/requests.filters';
import { AdminRequest } from '../shared/admin.models';
import {
    AUDIT_NO_FILTERS,
    auditApiParams,
    auditFiltered,
    auditFiltersFromQuery,
    auditFiltersToQuery,
    auditMetadata,
    AuditObjectContext,
    auditObjectLabel,
} from './audit.filters';

const ctx: AuditObjectContext = {
    users: new Map([['u-1', 'a.nikitina']]),
    tasks: new Map([['t-ci', 'Разбор упавшего CI-пайплайна']]),
    queues: new Map([['questions', 'Вопросы']]),
    requests: new Map([['q-1', 'Как собрать прошивку?']]),
};

describe('audit filters ↔ query', () => {
    it('round-trips filters through the URL', () => {
        const f = auditFiltersFromQuery({ period: '7d', user: 'd.orlov', action: 'users' });
        expect(f).toEqual({ period: { code: '7d' }, users: ['d.orlov'], actions: ['users'] });
        expect(auditFiltersToQuery(f)).toEqual({
            period: '7d',
            from: null,
            to: null,
            user: 'd.orlov',
            action: 'users',
        });
        expect(auditFiltered(f)).toBe(true);
    });

    it('keeps several users and categories as comma lists', () => {
        const f = auditFiltersFromQuery({ user: 'd.orlov, p.volkov,d.orlov', action: 'auth,bogus,queues' });
        expect(f).toEqual({ period: null, users: ['d.orlov', 'p.volkov'], actions: ['auth', 'queues'] });
        expect(auditFiltersToQuery(f)).toEqual(
            expect.objectContaining({ user: 'd.orlov,p.volkov', action: 'auth,queues' }),
        );
    });

    it('ignores unknown categories and empty values', () => {
        const f = auditFiltersFromQuery({ period: 'nope', user: '  ', action: 'bogus' });
        expect(f).toEqual(AUDIT_NO_FILTERS);
        expect(auditFiltered(f)).toBe(false);
        expect(auditFiltersToQuery(f)).toEqual({ period: null, from: null, to: null, user: null, action: null });
    });

    it('keeps a custom range', () => {
        const f = auditFiltersFromQuery({ period: 'custom', from: '2026-10-01T00:00:00Z', to: '2026-10-02T00:00:00Z' });
        expect(auditFiltersToQuery(f)).toEqual(
            expect.objectContaining({ period: 'custom', from: '2026-10-01T00:00:00Z', to: '2026-10-02T00:00:00Z' }),
        );
    });

    it('builds API params with category codes', () => {
        const params = auditApiParams(
            { period: null, users: ['p.volkov'], actions: ['auth'] },
            { from: '2026-10-18T00:00:00.000Z', to: null },
        );
        expect(params).toEqual({ from: '2026-10-18T00:00:00.000Z', user: 'p.volkov', action: 'auth.login' });
        expect(auditApiParams(AUDIT_NO_FILTERS, { from: null, to: null })).toEqual({});
    });

    it('joins several users and the codes of several categories', () => {
        const params = auditApiParams(
            { period: null, users: ['p.volkov', 'd.orlov'], actions: ['auth', 'queues'] },
            { from: null, to: null },
        );
        expect(params['user']).toBe('p.volkov,d.orlov');
        const codes = params['action'].split(',');
        expect(codes[0]).toBe('auth.login');
        expect(codes).toContain('queue.update');
        expect(codes).not.toContain('user.create');
    });
});

describe('audit object and metadata', () => {
    it('resolves objects by type, falls back to the raw id', () => {
        expect(auditObjectLabel({ object_type: 'user', object_id: 'u-1' }, ctx)).toBe('a.nikitina');
        expect(auditObjectLabel({ object_type: 'task', object_id: 't-ci' }, ctx)).toBe('Разбор упавшего CI-пайплайна');
        expect(auditObjectLabel({ object_type: 'queue', object_id: 'questions' }, ctx)).toBe('Очередь «Вопросы»');
        expect(auditObjectLabel({ object_type: 'setting', object_id: 'max_active_sessions' }, ctx)).toBe('Общий лимит');
        expect(auditObjectLabel({ object_type: 'consultation', object_id: 'q-1' }, ctx)).toBe('Как собрать прошивку?');
        expect(auditObjectLabel({ object_type: 'permission', object_id: 'per_7Hq2' }, ctx)).toBe('per_7Hq2');
        expect(auditObjectLabel({ object_type: null, object_id: null }, ctx)).toBe('—');
    });

    it('pretty-prints metadata, empty object means no details', () => {
        expect(auditMetadata('{"value":4}')).toBe('{\n  "value": 4\n}');
        expect(auditMetadata('{}')).toBeNull();
        expect(auditMetadata(null)).toBeNull();
        expect(auditMetadata('not json')).toBe('not json');
    });
});

describe('requests filter ↔ API params', () => {
    const r = (id: string, task_id: string | null) => ({ id, task_id }) as AdminRequest;

    it('joins lists and drops empty filters', () => {
        expect(
            requestFilterParams(
                { users: ['a.nikitina', 'p.volkov'], period: null, types: ['task', 'question'], tasks: ['t-ci'] },
                { from: '2026-10-18T00:00:00.000Z', to: null },
            ),
        ).toEqual({
            user: 'a.nikitina,p.volkov',
            type: 'task,question',
            task: 't-ci',
            from: '2026-10-18T00:00:00.000Z',
        });
        expect(requestFilterParams(REQUEST_NO_FILTERS, { from: null, to: null })).toEqual({});
    });

    it('drops the task when tasks are not among the types', () => {
        const f = { ...REQUEST_NO_FILTERS, types: ['question' as const], tasks: ['t-ci'] };
        expect(requestFilterParams(f, { from: null, to: null })).toEqual({ type: 'question' });
        expect(requestsByTasks([r('1', null), r('2', 't-mr')], { ...f, tasks: ['t-ci', 't-mr'] })).toHaveLength(2);
    });

    it('filters several tasks on the client: the API takes one', () => {
        const f = { ...REQUEST_NO_FILTERS, types: ['task' as const], tasks: ['t-ci', 't-mr'] };
        expect(requestFilterParams(f, { from: null, to: null })).toEqual({ type: 'task' });
        const items = [r('1', 't-ci'), r('2', 't-mr'), r('3', 't-notes'), r('4', null)];
        expect(requestsByTasks(items, f).map((x) => x.id)).toEqual(['1', '2']);
        // One task is filtered by the API; the client keeps what came back.
        expect(requestsByTasks(items, { ...f, tasks: ['t-ci'] })).toBe(items);
    });
});
