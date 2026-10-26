import { dur, durShort, plural } from './format';
import { NAME_REQUIRED, NAME_TAKEN, NAME_TOO_LONG, normalizeName, sortByName, validateName } from './names';
import { clashTitle, readClash } from './concurrency';
import { ApiError } from '../../../core/api/api-error';
import { SyncSubmit } from './sync-submit';

describe('names', () => {
    it('trims and NFC-normalizes', () => {
        expect(normalizeName('  Ёлка  ')).toBe('Ёлка');
        expect(normalizeName('Ёлка')).toBe('Ёлка');
        expect(normalizeName(null)).toBe('');
    });

    it('validates required, length and case-insensitive uniqueness', () => {
        expect(validateName('   ', [])).toBe(NAME_REQUIRED);
        expect(validateName('a'.repeat(121), [])).toBe(NAME_TOO_LONG);
        expect(validateName('a'.repeat(120), [])).toBeNull();
        expect(validateName(' задачи ', ['Задачи'])).toBe(NAME_TAKEN);
        expect(validateName('Ё', ['ё'])).toBe(NAME_TAKEN);
        expect(validateName('Новая', ['Задачи'])).toBeNull();
        expect(NAME_TOO_LONG).toBe('Не более 120 символов');
    });

    it('sorts with a numeric collator', () => {
        const names = ['Очередь 10', 'очередь 2', 'Альфа', 'Очередь 1'];
        expect(sortByName(names, (n) => n)).toEqual(['Альфа', 'Очередь 1', 'очередь 2', 'Очередь 10']);
    });
});

describe('format', () => {
    it('plural', () => {
        expect([1, 2, 5, 11, 12, 21, 22, 25, 111].map((n) => plural(n, 'запуск', 'запуска', 'запусков'))).toEqual([
            'запуск',
            'запуска',
            'запусков',
            'запусков',
            'запусков',
            'запуск',
            'запуска',
            'запусков',
            'запусков',
        ]);
    });

    it('durations', () => {
        expect(dur(102)).toBe('1 ч 42 мин');
        expect(dur(6 + 5 / 60)).toBe('6 мин 05 с');
        expect(dur(6)).toBe('6 мин');
        expect(dur(40 / 60)).toBe('40 с');
        expect(durShort(12.4)).toBe('12 мин');
        expect(durShort(0.5)).toBe('30 с');
    });
});

describe('concurrency', () => {
    const users = [{ id: 'u-pvolkov', login: 'p.volkov' }];

    it('reads a 409 conflict and a 404 deleted', () => {
        const conflict = new ApiError(409, 'conflict', null, { code: 'conflict', updated_by: 'p.volkov', updated_at: 'x' });
        expect(readClash(conflict)).toEqual({ kind: 'conflict', by: 'p.volkov', at: 'x' });
        expect(clashTitle(readClash(conflict)!, users)).toBe('Объект изменён пользователем p.volkov');
        const deleted = new ApiError(404, 'deleted', null, { code: 'deleted', deleted_by: 'u-pvolkov' });
        expect(clashTitle(readClash(deleted)!, users)).toBe('Объект удалён пользователем p.volkov');
        expect(readClash(new ApiError(404, null, 'Not found', 'Not found'))).toBeNull();
    });
});

describe('SyncSubmit', () => {
    beforeEach(() => vi.useFakeTimers());
    afterEach(() => vi.useRealTimers());

    it('warns after 30 s and «Отменить» stops waiting', async () => {
        const s = new SyncSubmit();
        const pending = s.run(() => new Promise<never>(() => undefined));
        expect(s.pending()).toBe(true);
        await vi.advanceTimersByTimeAsync(30_000);
        expect(s.slow()).toBe(true);
        s.waitMore();
        expect(s.slow()).toBe(false);
        await vi.advanceTimersByTimeAsync(29_000);
        expect(s.slow()).toBe(false);
        s.cancel();
        await expect(pending).resolves.toEqual({ ok: false, cancelled: true });
        expect(s.pending()).toBe(false);
    });

    it('puts a conflict into `clash` and other errors into `error`', async () => {
        const s = new SyncSubmit();
        await s.run(() => Promise.reject(new ApiError(409, 'conflict', null, { code: 'conflict', updated_by: 'p.volkov' })));
        expect(s.clash()?.kind).toBe('conflict');
        await s.run(() => Promise.reject(new ApiError(500, null, 'boom', 'boom')));
        expect(s.clash()).toBeNull();
        expect(s.error()?.text).toBe('boom');
    });
});
