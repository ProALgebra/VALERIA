import { plural } from './format';

/** One item of `POST /api/admin/users/batch` → `results`. */
export interface BatchResult {
    id: string;
    /** `null` when the id does not exist (`reason: 'not_found'`). */
    login: string | null;
    ok: boolean;
    skipped?: boolean;
    /** Mirrors `admin_batch_users` in app.py. */
    reason: null | 'self' | 'already' | 'not_found' | 'invalid' | 'workspace_in_use';
}

export type BatchAction = 'block' | 'unblock';

export interface BatchToast {
    kind: 'success' | 'warning' | 'error';
    title: string;
}

/**
 * Toast after block/unblock. `self` is not counted (the modal already says your account is untouched);
 * `already` counts as done (the target state is reached).
 * - all done: «Заблокированы 3 пользователя», «Заблокирован 1 пользователь»;
 * - partly: «Заблокированы 2 из 3: не удалось заблокировать p.volkov»;
 * - nothing: «Не удалось заблокировать p.volkov, m.belov».
 */
export function batchToast(action: BatchAction, results: readonly BatchResult[]): BatchToast | null {
    const counted = results.filter((r) => r.reason !== 'self');
    if (!counted.length) return null;
    const done = counted.filter((r) => r.ok || r.reason === 'already');
    const failed = counted.filter((r) => !(r.ok || r.reason === 'already'));
    const verb = action === 'block' ? 'заблокировать' : 'разблокировать';
    const failedList = failed.map((r) => r.login || r.id).join(', ');
    if (!failed.length) {
        const n = done.length;
        const word =
            action === 'block'
                ? plural(n, 'Заблокирован', 'Заблокированы', 'Заблокированы')
                : plural(n, 'Разблокирован', 'Разблокированы', 'Разблокированы');
        return { kind: 'success', title: `${word} ${n} ${plural(n, 'пользователь', 'пользователя', 'пользователей')}` };
    }
    if (!done.length) return { kind: 'error', title: `Не удалось ${verb} ${failedList}` };
    const word =
        action === 'block'
            ? plural(done.length, 'Заблокирован', 'Заблокированы', 'Заблокированы')
            : plural(done.length, 'Разблокирован', 'Разблокированы', 'Разблокированы');
    return { kind: 'warning', title: `${word} ${done.length} из ${counted.length}: не удалось ${verb} ${failedList}` };
}
