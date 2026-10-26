import { AdminTask, isOn, Options, TaskPayload } from '../config-shared/admin-config.api';
import { normalizeName, validateName } from '../config-shared/names';

export const CONTENT_REQUIRED = 'Укажите инструкцию';
export const HINT_MAX = 500;
export const HINT_TOO_LONG = `Не более ${HINT_MAX} символов`;

/** Values of the task form, as edited. */
export interface TaskFormValue {
    title: string;
    description: string;
    input_hint: string;
    content: string;
    agent: string;
    model: string;
    model_variant: string;
    queue_id: string;
    plan_first: boolean;
    visibility: 'all' | 'selected';
    user_ids: string[];
    next_task_ids: string[];
}

export type TaskFormErrors = Partial<Record<keyof TaskFormValue, string>>;

/** Defaults for «Создать задачу»: default agent and model from `/api/options`, the «Задачи» queue if it exists. */
export function emptyTask(options: Options | null, queueIds: readonly string[]): TaskFormValue {
    return {
        title: '',
        description: '',
        input_hint: '',
        content: '',
        agent: options?.default_agent ?? 'build',
        model: options?.default_model ?? options?.models[0]?.id ?? '',
        model_variant: '',
        queue_id: queueIds.includes('tasks') ? 'tasks' : (queueIds[0] ?? 'default'),
        plan_first: false,
        visibility: 'all',
        user_ids: [],
        next_task_ids: [],
    };
}

export function fromTask(t: AdminTask): TaskFormValue {
    return {
        title: t.title,
        description: t.description ?? '',
        input_hint: t.input_hint ?? '',
        content: t.content,
        agent: t.agent,
        model: t.model || `${t.provider_id}/${t.model_id}`,
        model_variant: t.model_variant ?? '',
        queue_id: t.queue_id,
        plan_first: isOn(t.plan_first),
        visibility: t.visibility,
        user_ids: [...(t.audience_user_ids ?? [])],
        next_task_ids: [...(t.next_task_ids ?? [])],
    };
}

const orNull = (s: string): string | null => s.trim() || null;

/**
 * The request body. `visibility` and `user_ids` always travel together in the same PATCH, so the audience is never
 * half-applied; `updated_at` is echoed exactly as the server sent it.
 */
export function toPayload(v: TaskFormValue, extra: { is_published?: boolean; updated_at?: string } = {}): TaskPayload {
    return {
        title: normalizeName(v.title),
        description: orNull(v.description),
        input_hint: orNull(v.input_hint),
        content: v.content.trim(),
        agent: v.agent,
        model: v.model,
        model_variant: orNull(v.model_variant),
        queue_id: v.queue_id,
        plan_first: v.plan_first,
        visibility: v.visibility,
        user_ids: v.visibility === 'selected' ? [...v.user_ids] : [],
        next_task_ids: [...v.next_task_ids],
        ...extra,
    };
}

export function validateTask(v: TaskFormValue, otherTitles: readonly string[]): TaskFormErrors {
    const errors: TaskFormErrors = {};
    const title = validateName(v.title, otherTitles);
    if (title) errors.title = title;
    if (!v.content.trim()) errors.content = CONTENT_REQUIRED;
    if (v.input_hint.trim().length > HINT_MAX) errors.input_hint = HINT_TOO_LONG;
    return errors;
}

/** Same content after the normalization the payload applies: used by the dirty check. */
export function sameTask(a: TaskFormValue, b: TaskFormValue): boolean {
    return JSON.stringify(toPayload(a)) === JSON.stringify(toPayload(b));
}

/** «Кто видит» column: «Все», «N пользователей», «—» for drafts. */
export function audienceLabel(t: AdminTask, plural: (n: number) => string): string {
    if (!isOn(t.is_published)) return '—';
    if (t.visibility === 'all') return 'Все';
    const n = t.audience_user_ids?.length ?? 0;
    return `${n} ${plural(n)}`;
}
