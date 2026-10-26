import { ApiError } from '../../../core/api/api-error';
import {
    readConflict,
    conflictText,
    accountType,
    accountTypeToApi,
    hasTaskAccess,
    byLastLogin,
    AdminUser,
} from './admin.models';
import { KbqPipeTypes } from '@koobiq/components/filter-bar';
import { multiSelectValues, pipeTrackKey, withEpoch } from './pipe-track';
import { auditCategoryCodes, auditLabel, AUDIT_ACTIONS } from './audit-labels';
import { batchToast, BatchResult } from './batch';
import { formatWhen, plural, formatDay } from './format';
import {
    listFromQuery,
    listToQuery,
    periodFromPipe,
    periodFromQuery,
    periodRange,
    periodToPipe,
    periodToQuery,
    PERIODS,
} from './period';

const now = new Date(2026, 9, 24, 14, 30);

describe('format', () => {
    it('plural follows Russian rules', () => {
        const w = (n: number) => plural(n, 'пользователь', 'пользователя', 'пользователей');
        expect([1, 2, 5, 11, 12, 21, 22, 25, 111, 104].map(w)).toEqual([
            'пользователь',
            'пользователя',
            'пользователей',
            'пользователей',
            'пользователей',
            'пользователь',
            'пользователя',
            'пользователей',
            'пользователей',
            'пользователя',
        ]);
    });

    it('formats today, yesterday and older dates like the prototype', () => {
        expect(formatWhen(new Date(2026, 9, 24, 14, 21).toISOString(), { now })).toBe('Сегодня, 14:21');
        expect(formatWhen(new Date(2026, 9, 23, 18, 2).toISOString(), { now })).toBe('Вчера, 18:02');
        expect(formatWhen(new Date(2026, 9, 21, 9, 31).toISOString(), { now })).toBe('21 окт');
        expect(formatWhen(new Date(2026, 9, 21, 9, 31).toISOString(), { now, timeForOld: true })).toBe('21 окт, 09:31');
        expect(formatWhen(new Date(2025, 8, 15).toISOString(), { now })).toBe('15 сен 2025');
        expect(formatWhen(null, { now })).toBe('—');
        expect(formatDay('2026-05-02T10:00:00.000000+00:00', now)).toBe('2 мая');
    });
});

describe('period ↔ query', () => {
    it('round-trips presets through the URL and the pipe', () => {
        for (const p of PERIODS) {
            const state = periodFromQuery(p.id);
            expect(state).toEqual({ code: p.id });
            expect(periodToQuery(state)).toEqual({ period: p.id, from: null, to: null });
            expect(periodFromPipe(periodToPipe(state))).toEqual(state);
        }
    });

    it('round-trips a custom range', () => {
        const state = periodFromQuery('custom', '2026-10-01T00:00:00.000Z', '2026-10-02T00:00:00.000Z');
        expect(state).toEqual({ from: '2026-10-01T00:00:00.000Z', to: '2026-10-02T00:00:00.000Z' });
        expect(periodToQuery(state)).toEqual({
            period: 'custom',
            from: '2026-10-01T00:00:00.000Z',
            to: '2026-10-02T00:00:00.000Z',
        });
        expect(periodFromPipe(periodToPipe(state))).toEqual(state);
    });

    it('ignores unknown codes and broken ranges', () => {
        expect(periodFromQuery('5y')).toBeNull();
        expect(periodFromQuery('custom', 'x', null)).toBeNull();
        expect(periodFromQuery(null)).toBeNull();
        expect(periodToQuery(null)).toEqual({ period: null, from: null, to: null });
        expect(periodFromPipe(null)).toBeNull();
    });

    it('computes API from/to', () => {
        expect(periodRange({ code: '1h' }, now)).toEqual({
            from: new Date(now.getTime() - 3_600_000).toISOString(),
            to: null,
        });
        expect(periodRange(null, now)).toEqual({ from: null, to: null });
    });

    it('maps comma lists', () => {
        expect(listFromQuery('a, b,,c')).toEqual(['a', 'b', 'c']);
        expect(listFromQuery(null)).toEqual([]);
        expect(listToQuery(['a', 'b'])).toBe('a,b');
        expect(listToQuery([])).toBeNull();
    });
});

describe('batchToast', () => {
    const r = (login: string, ok: boolean, reason: BatchResult['reason'] = null): BatchResult => ({
        id: login,
        login,
        ok,
        reason,
    });

    it('all blocked', () => {
        expect(batchToast('block', [r('a', true), r('b', true), r('c', true)])).toEqual({
            kind: 'success',
            title: 'Заблокированы 3 пользователя',
        });
        expect(batchToast('block', [r('a', true)])).toEqual({ kind: 'success', title: 'Заблокирован 1 пользователь' });
        expect(
            batchToast(
                'unblock',
                Array.from({ length: 5 }, (_, i) => r(`u${i}`, true)),
            ),
        ).toEqual({
            kind: 'success',
            title: 'Разблокированы 5 пользователей',
        });
        expect(
            batchToast(
                'block',
                Array.from({ length: 21 }, (_, i) => r(`u${i}`, true)),
            )?.title,
        ).toBe('Заблокирован 21 пользователь');
    });

    it('partial success names the failed logins', () => {
        expect(batchToast('block', [r('a.nikitina', true), r('m.belov', true), r('p.volkov', false, 'invalid')])).toEqual(
            {
                kind: 'warning',
                title: 'Заблокированы 2 из 3: не удалось заблокировать p.volkov',
            },
        );
    });

    it('nothing done is an error', () => {
        expect(batchToast('unblock', [r('p.volkov', false, 'invalid'), r('m.belov', false, 'not_found')])).toEqual({
            kind: 'error',
            title: 'Не удалось разблокировать p.volkov, m.belov',
        });
    });

    it('a workspace taken by another active user is a failed unblock', () => {
        expect(batchToast('unblock', [r('a', true), r('p.volkov', false, 'workspace_in_use')])).toEqual({
            kind: 'warning',
            title: 'Разблокирован 1 из 2: не удалось разблокировать p.volkov',
        });
        expect(batchToast('unblock', [r('p.volkov', false, 'workspace_in_use')])).toEqual({
            kind: 'error',
            title: 'Не удалось разблокировать p.volkov',
        });
    });

    it('does not count self and counts already as done', () => {
        expect(batchToast('block', [r('d.orlov', false, 'self'), r('a', true, 'already'), r('b', true)])).toEqual({
            kind: 'success',
            title: 'Заблокированы 2 пользователя',
        });
        expect(batchToast('block', [r('d.orlov', false, 'self')])).toBeNull();
    });
});

describe('audit labels', () => {
    it('uses prototype labels and falls back to the code', () => {
        expect(auditLabel('auth.login')).toBe('Вошёл в VALERIA');
        expect(auditLabel('concurrency.update')).toBe('Изменил общий лимит');
        expect(auditLabel('something.new')).toBe('something.new');
    });

    it('covers every API.md §3.3 action', () => {
        const api =
            'admin.bootstrap auth.login chats.admin_list concurrency.reset concurrency.update consultation.admin_view consultation.question.reply consultation.settings.update job.stop.completed job.stop.request job.stop.retry message.impersonated permission.admin_response question.reply queue.archive queue.create queue.update task.audience.update task.create task.delete task.pipeline.run task.run task.update task_access.admin_list task_access.update task_run.messages task_run.result task_runs.admin_list template.create template.delete template.update user.create user.password_reset user.profile_update user.update';
        for (const code of api.split(' ')) expect(AUDIT_ACTIONS[code]).toBeDefined();
    });

    it('maps categories to code lists', () => {
        expect(auditCategoryCodes('auth')).toEqual(['auth.login']);
        expect(auditCategoryCodes('imp')).toContain('consultation.admin_view');
        expect(auditCategoryCodes('users')).toContain('user.password_reset');
    });
});

describe('admin models', () => {
    it('maps account types', () => {
        expect(accountType({ role: 'admin', tasks_only: 1 })).toBe('admin');
        expect(accountType({ role: 'user', tasks_only: 1 })).toBe('tasks');
        expect(accountType({ role: 'user', tasks_only: 0 })).toBe('user');
        expect(accountTypeToApi('tasks')).toEqual({ role: 'user', tasks_only: true });
    });

    it('computes task access', () => {
        expect(
            hasTaskAccess({ id: 't', title: '', visibility: 'all', is_published: 1, audience_user_ids: [] }, 'u'),
        ).toBe(true);
        expect(
            hasTaskAccess({ id: 't', title: '', visibility: 'selected', is_published: 1, audience_user_ids: [7] }, 7),
        ).toBe(true);
        expect(
            hasTaskAccess({ id: 't', title: '', visibility: 'selected', is_published: 1, audience_user_ids: [] }, 'u'),
        ).toBe(false);
    });

    it('reads conflicts and resolves raw ids to logins', () => {
        const err = new ApiError(409, 'conflict', null, {
            code: 'conflict',
            updated_by: 'u-pvolkov',
            updated_at: '2026-10-24T14:00:00.123456+00:00',
        });
        const info = readConflict(err, [{ id: 'u-pvolkov', login: 'p.volkov' }]);
        expect(info).toEqual({ kind: 'conflict', by: 'p.volkov', updatedAt: '2026-10-24T14:00:00.123456+00:00' });
        expect(conflictText(info!)).toBe('Объект изменён пользователем p.volkov');
        const gone = readConflict(new ApiError(404, 'deleted', null, { code: 'deleted', deleted_by: 'd.orlov' }));
        expect(conflictText(gone!)).toBe('Объект удалён пользователем d.orlov');
        expect(readConflict(new ApiError(409, null, 'Workspace cannot change', 'Workspace cannot change'))).toBeNull();
    });
});

describe('byLastLogin', () => {
    const u = (login: string, last_login_at: string | null) => ({ login, last_login_at }) as AdminUser;
    it('puts the newest login first and never-logged-in users last', () => {
        const sorted = [
            u('youtrack-bot', null),
            u('a.nikitina', '2026-10-23T10:00:00Z'),
            u('d.orlov', '2026-10-24T09:12:00Z'),
            u('i.sokolova', '2026-10-24T13:50:00Z'),
        ].sort(byLastLogin);
        expect(sorted.map((x) => x.login)).toEqual(['i.sokolova', 'd.orlov', 'a.nikitina', 'youtrack-bot']);
    });
});

describe('pipeTrackKey', () => {
    const pipe = (type: KbqPipeTypes, value: unknown) =>
        ({ name: 'Задача', id: 'task', type, value, cleanable: true, removable: false, disabled: false }) as never;

    it('changes for a single select once its value label resolves', () => {
        const before = pipeTrackKey(pipe(KbqPipeTypes.Select, null));
        const after = pipeTrackKey(pipe(KbqPipeTypes.Select, { name: 'Разбор упавшего CI-пайплайна', value: 't-ci' }));
        expect(before).toBe('0|task|');
        expect(after).toBe('0|task|Разбор упавшего CI-пайплайна');
    });

    it('stays stable for multi-select and datetime pipes while their values change', () => {
        expect(pipeTrackKey(pipe(KbqPipeTypes.MultiSelect, [{ name: 'a', value: 'a' }]))).toBe('0|task');
        expect(pipeTrackKey(pipe(KbqPipeTypes.MultiSelect, []))).toBe('0|task');
        expect(pipeTrackKey(pipe(KbqPipeTypes.Datetime, { start: { days: -30 }, end: null }))).toBe('0|task');
    });

    it('changes for every pipe built in a new epoch (reset, late labels)', () => {
        const [multi, date] = withEpoch([pipe(KbqPipeTypes.MultiSelect, []), pipe(KbqPipeTypes.Datetime, null)], 2);
        expect(pipeTrackKey(multi)).toBe('2|task');
        expect(pipeTrackKey(date)).toBe('2|task');
    });

    it('keeps the epoch on the pipe object, so a rendered pipe keeps its old key', () => {
        const old = withEpoch([pipe(KbqPipeTypes.MultiSelect, [])], 1)[0];
        const fresh = withEpoch([pipe(KbqPipeTypes.MultiSelect, [])], 2)[0];
        expect(pipeTrackKey(old)).toBe('1|task');
        expect(pipeTrackKey(fresh)).toBe('2|task');
    });
});

describe('multiSelectValues', () => {
    it('maps picked options to their values; anything else is empty', () => {
        expect(multiSelectValues([{ name: 'A', value: 'a' }, { name: 'B', value: 2 }])).toEqual(['a', '2']);
        expect(multiSelectValues([])).toEqual([]);
        expect(multiSelectValues(null)).toEqual([]);
    });
});
