import { ConsultationSettings, isOn, QueueOverview, QueueRow } from '../config-shared/admin-config.api';
import { plural } from '../config-shared/format';
import { NAME_MAX, NAME_REQUIRED, NAME_TAKEN, NAME_TOO_LONG, nameCollator, normalizeName } from '../config-shared/names';

export interface QueueFormRow {
    /** `null` for a queue added in this form. */
    id: string | null;
    /** Stable key for the table: the id, or `new-N` for added rows. */
    key: string;
    name: string;
    priority: number | null;
    max_workers: number | null;
    is_system: boolean;
    updated_at: string | null;
    active: number;
    queued: number;
}

export interface QueueFormValue {
    limit: number | null;
    /** `true` — the limit comes from the server configuration (no override). */
    useEnv: boolean;
    rows: QueueFormRow[];
    /** Ids of saved queues marked «Архивировать»; applied on «Сохранить». */
    archived: string[];
    consultQueue: string;
    consultModel: string;
    chatQueue: string;
}

export type QueueFormErrors = Record<string, string>;

export const sessions = (n: number) => plural(n, 'сессия', 'сессии', 'сессий');
export const unfinished = (n: number) =>
    n === 1 ? 'одно незавершённое задание' : `${n} ${plural(n, 'незавершённое задание', 'незавершённых задания', 'незавершённых заданий')}`;

/**
 * The limit is overridden when it differs from the configuration value. The API has no explicit flag,
 * so an override equal to the configuration value reads as «from configuration».
 */
export const isOverridden = (ov: Pick<QueueOverview, 'limit' | 'env_limit'>) => ov.limit !== ov.env_limit;

export function toRow(q: QueueRow): QueueFormRow {
    return {
        id: q.id,
        key: q.id,
        name: q.name,
        priority: q.priority,
        max_workers: q.max_workers,
        is_system: isOn(q.is_system),
        updated_at: q.updated_at,
        active: q.active,
        queued: q.queued,
    };
}

export function fromOverview(ov: QueueOverview, consult: ConsultationSettings): QueueFormValue {
    return {
        limit: ov.limit,
        useEnv: !isOverridden(ov),
        rows: ov.queues.filter((q) => q.status === 'active').map(toRow),
        archived: [],
        consultQueue: consult.queue_id,
        consultModel: consult.model,
        chatQueue: ov.default_chat_queue_id,
    };
}

/** Rows shown in the table: archived ones are gone, sorted by priority like the server does. */
export function visibleRows(v: QueueFormValue): QueueFormRow[] {
    return v.rows.filter((r) => !r.id || !v.archived.includes(r.id));
}

/** «Используется»: who sends requests to this queue. */
export function usedBy(queueId: string, ctx: { consultQueue: string; chatQueue: string; taskQueues: readonly string[] }): string {
    const parts: string[] = [];
    if (ctx.consultQueue === queueId) parts.push('вопросы');
    if (ctx.chatQueue === queueId) parts.push('работа в папке');
    const n = ctx.taskQueues.filter((q) => q === queueId).length;
    if (n) parts.push(`${n} ${plural(n, 'задача', 'задачи', 'задач')}`);
    return parts.join(', ');
}

/** Why a queue cannot be archived, or `null`. Texts follow the prototype alert. */
export function archiveBlock(row: QueueFormRow, used: string): { title: string; text: string } | null {
    const busy = row.active + row.queued;
    if (!used && !busy) return null;
    const title = `Очередь «${row.name}» нельзя архивировать`;
    if (used && busy) {
        return { title, text: `Её используют ${used}, и в ней ${unfinished(busy)}. Перенесите задачи в другую очередь в их настройках и дождитесь окончания заданий.` };
    }
    if (used) return { title, text: `Её используют ${used}. Перенесите задачи в другую очередь в их настройках.` };
    return { title, text: `В ней ${unfinished(busy)}. Дождитесь окончания заданий.` };
}

const isInt = (n: number | null): n is number => n !== null && Number.isInteger(n);

export function validateQueues(v: QueueFormValue, hardLimit: number): QueueFormErrors {
    const errors: QueueFormErrors = {};
    const range = `От 1 до ${hardLimit}`;
    if (!v.useEnv && (!isInt(v.limit) || v.limit < 1 || v.limit > hardLimit)) errors['limit'] = range;
    const rows = visibleRows(v);
    rows.forEach((r) => {
        const name = normalizeName(r.name);
        if (!name) errors[`${r.key}:name`] = NAME_REQUIRED;
        else if (name.length > NAME_MAX) errors[`${r.key}:name`] = NAME_TOO_LONG;
        else if (rows.some((o) => o !== r && nameCollator.compare(normalizeName(o.name), name) === 0)) errors[`${r.key}:name`] = NAME_TAKEN;
        if (r.priority !== null && !Number.isInteger(r.priority)) errors[`${r.key}:priority`] = 'Целое число';
        if (!isInt(r.max_workers) || r.max_workers < 1 || r.max_workers > hardLimit) errors[`${r.key}:max_workers`] = range;
    });
    return errors;
}

export type QueueRequest =
    | { kind: 'concurrency'; value: number }
    | { kind: 'concurrency-reset' }
    | { kind: 'create'; key: string; body: { name: string; priority: number; max_workers: number } }
    | { kind: 'patch'; id: string; body: { name: string; priority: number; max_workers: number; updated_at: string } }
    | { kind: 'consultations'; body: { model: string; queue_id: string } }
    | { kind: 'chat-queue'; queue_id: string }
    | { kind: 'archive'; id: string };

/**
 * Turns the edited form into requests, in the order they must run: limit, new and changed queues, routing
 * (so a queue stops being used before it is archived), then archiving.
 */
export function planRequests(initial: QueueFormValue, v: QueueFormValue): QueueRequest[] {
    const out: QueueRequest[] = [];
    if (v.useEnv) {
        if (!initial.useEnv) out.push({ kind: 'concurrency-reset' });
    } else if (initial.useEnv || v.limit !== initial.limit) {
        out.push({ kind: 'concurrency', value: v.limit! });
    }
    const body = (r: QueueFormRow) => ({ name: normalizeName(r.name), priority: r.priority ?? 0, max_workers: r.max_workers! });
    for (const r of visibleRows(v)) {
        if (!r.id) {
            out.push({ kind: 'create', key: r.key, body: body(r) });
            continue;
        }
        const was = initial.rows.find((o) => o.id === r.id);
        const b = body(r);
        if (!was || b.name !== was.name || b.priority !== (was.priority ?? 0) || b.max_workers !== was.max_workers) {
            out.push({ kind: 'patch', id: r.id, body: { ...b, updated_at: r.updated_at ?? '' } });
        }
    }
    if (v.consultQueue !== initial.consultQueue || v.consultModel !== initial.consultModel) {
        out.push({ kind: 'consultations', body: { model: v.consultModel, queue_id: v.consultQueue } });
    }
    if (v.chatQueue !== initial.chatQueue) out.push({ kind: 'chat-queue', queue_id: v.chatQueue });
    for (const id of v.archived) out.push({ kind: 'archive', id });
    return out;
}
