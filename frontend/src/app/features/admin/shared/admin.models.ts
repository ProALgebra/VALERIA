import { ApiError } from '../../../core/api/api-error';

/** Row of `GET /api/admin/users` (string ids in the mock, numbers in the backend). */
export interface AdminUser {
    id: string | number;
    login: string;
    role: 'user' | 'admin' | string;
    tasks_only: boolean | number;
    status: 'active' | 'disabled' | string;
    workspace_path: string | null;
    mattermost_username: string | null;
    max_active_jobs: number | null;
    created_at: string | null;
    updated_at: string;
    last_login_at: string | null;
}

/** Row of `GET /api/admin/tasks` (only the fields these screens read). */
export interface AdminTask {
    id: string;
    title: string;
    visibility: 'all' | string;
    is_published: boolean | number;
    audience_user_ids: (string | number)[];
}

/** Row of `GET /api/admin/requests`. */
export interface AdminRequest {
    kind: 'question' | 'task' | 'folder';
    id: string;
    chat_id: string;
    title: string;
    status: string;
    user: { id: string | number; login: string | null };
    task_id: string | null;
    task_title: string | null;
    created_at: string;
    updated_at: string;
}

/** Job row (`GET /api/jobs/:id`, `GET /api/admin/queue` → jobs). */
export interface AdminJob {
    id: string;
    chat_id: string;
    effective_user_id: string | number;
    effective_login?: string | null;
    status: string;
    task_id: string | null;
    task_title: string | null;
    chat_title?: string | null;
    queue_name?: string | null;
    provider_id?: string | null;
    model_id?: string | null;
    error_code?: string | null;
    created_at: string;
    finished_at?: string | null;
}

export type AccountType = 'user' | 'tasks' | 'admin';

/** «Тип учётки» of a user. */
export function accountType(user: Pick<AdminUser, 'role' | 'tasks_only'>): AccountType {
    if (user.role === 'admin') return 'admin';
    return user.tasks_only ? 'tasks' : 'user';
}

/** «Тип учётки» options in form order; the admin option has no caption. */
export const ACCOUNT_TYPES: readonly { id: AccountType; label: string; hint?: string }[] = [
    { id: 'tasks', label: 'Задачи и вопросы', hint: 'Без работы в папке' },
    { id: 'user', label: 'Пользователь', hint: 'Вопросы, задачи и работа в своей папке' },
    { id: 'admin', label: 'Администратор' },
];

export function accountTypeLabel(type: AccountType): string {
    return ACCOUNT_TYPES.find((t) => t.id === type)!.label;
}

/** `role` + `tasks_only` for the API. */
export function accountTypeToApi(type: AccountType): { role: 'user' | 'admin'; tasks_only: boolean } {
    return { role: type === 'admin' ? 'admin' : 'user', tasks_only: type === 'tasks' };
}

export function isBlocked(user: Pick<AdminUser, 'status'>): boolean {
    return user.status !== 'active';
}

/** Whether a task is available to the user: open to all or the user is in its audience. */
export function hasTaskAccess(task: AdminTask, userId: string | number): boolean {
    return task.visibility === 'all' || task.audience_user_ids.some((id) => String(id) === String(userId));
}

/** Statuses that keep a job in the queue or at work (the workspace cannot change while any exists). */
export const UNFINISHED_JOB_STATUSES: readonly string[] = [
    'queued',
    'dispatching',
    'running',
    'waiting_permission',
    'waiting_question',
    'cancelling',
];

/** Optimistic-concurrency payload of a 409 `conflict` / 404 `deleted`. */
export interface ConflictInfo {
    kind: 'conflict' | 'deleted';
    /** Login (or raw id if the backend sent an id) of who changed or deleted the object. */
    by: string | null;
    /** Fresh `updated_at` to resend with «Перезаписать», exactly as received. */
    updatedAt: string | null;
}

/** Reads a conflict / deletion from an error; null for anything else. `users` resolves raw ids to logins. */
export function readConflict(
    err: unknown,
    users: readonly Pick<AdminUser, 'id' | 'login'>[] = [],
): ConflictInfo | null {
    if (!(err instanceof ApiError)) return null;
    const payload = (err.payload && typeof err.payload === 'object' ? err.payload : {}) as Record<string, unknown>;
    const resolve = (raw: unknown): string | null => {
        if (raw === null || raw === undefined || raw === '') return null;
        const found = users.find((u) => String(u.id) === String(raw));
        return found ? found.login : String(raw);
    };
    if (err.isConflict && err.code === 'conflict') {
        return {
            kind: 'conflict',
            by: resolve(payload['updated_by']),
            updatedAt: typeof payload['updated_at'] === 'string' ? (payload['updated_at'] as string) : null,
        };
    }
    if (err.isNotFound && err.code === 'deleted')
        return { kind: 'deleted', by: resolve(payload['deleted_by']), updatedAt: null };
    return null;
}

/** Text of the concurrency alert. */
export function conflictText(info: ConflictInfo): string {
    if (info.kind === 'deleted') return info.by ? `Объект удалён пользователем ${info.by}` : 'Объект удалён';
    return info.by ? `Объект изменён пользователем ${info.by}` : 'Объект изменён другим пользователем';
}

/** Newest «Последний вход» first; users who never logged in («—») last, then by login. */
export function byLastLogin(a: AdminUser, b: AdminUser): number {
    const ta = a.last_login_at ? Date.parse(a.last_login_at) : -Infinity;
    const tb = b.last_login_at ? Date.parse(b.last_login_at) : -Infinity;
    if (ta !== tb) return tb > ta ? 1 : -1;
    return a.login.localeCompare(b.login);
}
