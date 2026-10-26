import { ApiError } from '../../../core/api/api-error';

/** Result of a save that hit another admin's change (SPEC §15, concurrent edit). */
export type Clash = { kind: 'conflict'; by: string | null; at: string | null } | { kind: 'deleted'; by: string | null };

/** 409 `{code:"conflict", updated_by, updated_at}` or 404 `{code:"deleted", deleted_by}` → Clash; anything else → null. */
export function readClash(err: unknown): Clash | null {
    if (!(err instanceof ApiError)) return null;
    const p = (err.payload && typeof err.payload === 'object' ? err.payload : {}) as Record<string, unknown>;
    const str = (v: unknown) => (typeof v === 'string' && v ? v : null);
    if (err.isConflict && err.code === 'conflict') return { kind: 'conflict', by: str(p['updated_by']), at: str(p['updated_at']) };
    if (err.isNotFound && err.code === 'deleted') return { kind: 'deleted', by: str(p['deleted_by']) };
    return null;
}

/** 409 bodies carry a login; payload `updated_by` is a raw id. Either way show a login when we can. */
export function loginOf(idOrLogin: string | null, users: readonly { id: string; login: string }[]): string | null {
    if (!idOrLogin) return null;
    return users.find((u) => u.id === idOrLogin)?.login ?? idOrLogin;
}

export function clashTitle(clash: Clash, users: readonly { id: string; login: string }[]): string {
    const who = loginOf(clash.by, users);
    if (clash.kind === 'conflict') return who ? `Объект изменён пользователем ${who}` : 'Объект изменён другим пользователем';
    return who ? `Объект удалён пользователем ${who}` : 'Объект удалён';
}
