import { KbqBadgeColors } from '@koobiq/components/badge';

/** Chat kinds in `/api/me/items`. */
export type ItemKind = 'question' | 'task' | 'folder';

export type ItemStatus =
    | 'waiting_question'
    | 'waiting_permission'
    | 'queued'
    | 'dispatching'
    | 'running'
    | 'cancelling'
    | 'completed'
    | 'idle'
    | 'failed'
    | 'cancelled';

/** Status colour from SPEC §9. `default` is the neutral badge. */
export type StatusTone = 'warning' | 'default' | 'theme' | 'success' | 'error';

/** «Ждёт вашего ответа» / «Ждёт разрешения»: the chat needs the user. */
export const WAITING_STATUSES: readonly string[] = ['waiting_question', 'waiting_permission'];
/** In progress on the server. */
export const WORKING_STATUSES: readonly string[] = ['queued', 'dispatching', 'running', 'cancelling'];

export function isWaiting(status: string): boolean {
    return WAITING_STATUSES.includes(status);
}

export function isWorking(status: string): boolean {
    return WORKING_STATUSES.includes(status);
}

/** Active = waiting for the user or still working (SPEC §6.1). Everything else is «Готово». */
export function isActive(status: string): boolean {
    return isWaiting(status) || isWorking(status);
}

const SIMPLE: Record<string, [string, StatusTone]> = {
    waiting_question: ['Ждёт вашего ответа', 'warning'],
    waiting_permission: ['Ждёт разрешения', 'warning'],
    queued: ['В очереди', 'default'],
    dispatching: ['Подготовка окружения', 'default'],
    running: ['В работе', 'theme'],
    cancelling: ['Отменяется', 'default'],
    failed: ['Завершена с ошибкой', 'error'],
    cancelled: ['Отменена', 'default'],
};

/**
 * Human label for a status (SPEC §9). «Готовый» status depends on the kind:
 * question completed → «Ответ готов», task completed → «Завершена», folder idle → «Готово».
 */
export function statusLabel(status: string, kind?: ItemKind | string): string {
    return describe(status, kind)[0];
}

/**
 * Status label for admin views: the admin looks at someone else's session, so «Ждёт вашего ответа»
 * would read as addressed to them. `waiting_question` → «Ждёт пользователя»; everything else as `statusLabel`.
 */
export function adminStatusLabel(status: string, kind?: ItemKind | string): string {
    return status === 'waiting_question' ? 'Ждёт пользователя' : statusLabel(status, kind);
}

export function statusTone(status: string, kind?: ItemKind | string): StatusTone {
    return describe(status, kind)[1];
}

const BADGE: Record<StatusTone, KbqBadgeColors> = {
    warning: KbqBadgeColors.FadeWarning,
    default: KbqBadgeColors.FadeContrast,
    theme: KbqBadgeColors.FadeTheme,
    success: KbqBadgeColors.FadeSuccess,
    error: KbqBadgeColors.FadeError,
};

/** `kbq-badge [badgeColor]` for a status. */
export function statusBadgeColor(status: string, kind?: ItemKind | string): KbqBadgeColors {
    return BADGE[statusTone(status, kind)];
}

export type StatusIcon = 'waiting' | 'running' | 'success' | 'failed' | 'cancelled' | 'pending';

/** Icon shown next to a chat in the menu (prototype `mark()`). */
export function statusIcon(status: string, kind?: ItemKind | string): StatusIcon {
    if (isWaiting(status)) return 'waiting';
    if (status === 'running') return 'running';
    if (status === 'failed') return 'failed';
    if (status === 'cancelled') return 'cancelled';
    if (statusTone(status, kind) === 'success') return 'success';
    return 'pending';
}

function describe(status: string, kind?: ItemKind | string): [string, StatusTone] {
    const simple = SIMPLE[status];
    if (simple) return simple;
    if (status === 'completed' || status === 'idle') {
        if (kind === 'question') return ['Ответ готов', 'success'];
        if (kind === 'folder') return ['Готово', 'success'];
        if (kind === 'task') return ['Завершена', 'success'];
        return status === 'idle' ? ['Готово', 'success'] : ['Завершена', 'success'];
    }
    return [status, 'default'];
}

/** Kind icons (SPEC §6.1): question = message, task = zap, folder = wrench with play. */
export const KIND_ICON: Record<ItemKind, string> = {
    question: 'kbq-message_16',
    task: 'kbq-bolt_16',
    folder: 'kbq-wrench-badge-play_16',
};
