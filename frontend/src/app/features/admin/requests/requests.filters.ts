import { ItemKind } from '../../../core/status/status';
import { AdminRequest } from '../shared/admin.models';
import { PeriodState } from '../shared/period';

/**
 * Filters of «Запросы пользователей» as they live in the URL (search `q` is client-side and not here).
 * Several values of one filter are OR, filters are AND; an empty list means «all».
 */
export interface RequestFilters {
    users: string[];
    period: PeriodState;
    types: ItemKind[];
    /** Task ids; only meaningful while «Задача» is among the types. */
    tasks: string[];
}

export const REQUEST_NO_FILTERS: RequestFilters = { users: [], period: null, types: [], tasks: [] };

/** Task ids that apply: none while the types are picked and «Задача» is not among them. */
function activeTasks(filters: RequestFilters): string[] {
    return !filters.types.length || filters.types.includes('task') ? filters.tasks : [];
}

/**
 * Query params for `GET /api/admin/requests`; empty filters are left out. The API takes one `task`: several
 * tasks are not sent and are filtered on the client by {@link requestsByTasks}.
 */
export function requestFilterParams(
    filters: RequestFilters,
    range: { from: string | null; to: string | null },
): Record<string, string> {
    const params: Record<string, string> = {};
    const tasks = activeTasks(filters);
    if (filters.users.length) params['user'] = filters.users.join(',');
    if (filters.types.length) params['type'] = filters.types.join(',');
    if (tasks.length === 1) params['task'] = tasks[0];
    if (range.from) params['from'] = range.from;
    if (range.to) params['to'] = range.to;
    return params;
}

/** The client-side part of the «Задача» filter: requests of any of several tasks. */
export function requestsByTasks(items: AdminRequest[], filters: RequestFilters): AdminRequest[] {
    const tasks = activeTasks(filters);
    if (tasks.length < 2) return items;
    return items.filter((r) => !!r.task_id && tasks.includes(r.task_id));
}
