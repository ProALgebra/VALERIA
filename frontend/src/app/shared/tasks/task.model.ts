import { isActive } from '../../core/status/status';

/** Task from `GET /api/tasks`: what the user can launch. The instruction itself is hidden. */
export interface TaskDef {
    id: string | number;
    title: string;
    description: string | null;
    plan_first: boolean | number;
    input_hint: string | null;
}

/** `public_task_run` from `/api/task-runs*` and `POST /api/tasks/:id/runs`. */
export interface TaskRun {
    id: string | number;
    task_id: string | number;
    plan_first: boolean | number;
    /** `plan` while a plan_first task is planning, then `build`. */
    phase: string | null;
    /** Display title: task title, later «task title: short summary». */
    title: string;
    status: string;
    user_input: string | null;
    error_code: string | null;
    created_at: string;
    started_at: string | null;
    finished_at: string | null;
}

/** `GET /api/task-runs/:id/result`. */
export interface TaskRunResult {
    run_id: string | number;
    status: 'available' | 'unavailable' | 'not_completed' | string;
    text: string | null;
}

/** Published template from `GET /api/templates`: text for the «Сделать» field. */
export interface TemplateDef {
    id: string | number;
    title: string;
    description: string | null;
    content: string;
}

export function sameId(a: string | number | null | undefined, b: string | number | null | undefined): boolean {
    return a !== null && a !== undefined && b !== null && b !== undefined && String(a) === String(b);
}

/**
 * Client-side duplicate check before «Запустить» (SPEC §8.1): an unfinished run of the same task
 * with the same addition (trimmed). Returns the newest one, or null.
 */
export function findActiveDuplicate(
    runs: readonly TaskRun[],
    taskId: string | number,
    userInput: string,
): TaskRun | null {
    const input = userInput.trim();
    let found: TaskRun | null = null;
    for (const run of runs) {
        if (!sameId(run.task_id, taskId) || !isActive(run.status)) continue;
        if ((run.user_input ?? '').trim() !== input) continue;
        if (!found || Date.parse(run.created_at) > Date.parse(found.created_at)) found = run;
    }
    return found;
}

/** `HH:mm` in local time. */
export function clockTime(iso: string | null | undefined): string {
    if (!iso) return '';
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return '';
    return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

const MONTHS_SHORT = ['янв', 'фев', 'мар', 'апр', 'мая', 'июн', 'июл', 'авг', 'сен', 'окт', 'ноя', 'дек'];

/** «17 окт», as the prototype writes dates (no trailing dot). */
export function shortDate(d: Date): string {
    return `${d.getDate()} ${MONTHS_SHORT[d.getMonth()]}`;
}
