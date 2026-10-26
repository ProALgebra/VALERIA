import { ItemKind, StatusTone, statusTone } from '../../../core/status/status';

export const KIND_LABEL: Record<ItemKind, string> = {
    question: 'Вопрос',
    task: 'Задача',
    folder: 'Работа в папке',
};

/** Text colour for a status line outside a badge (prototype `.v-crow__st`). */
export function statusTextColor(status: string, kind?: string): string {
    const tone: StatusTone = statusTone(status, kind);
    if (tone === 'warning') return 'var(--kbq-foreground-warning)';
    if (tone === 'success') return 'var(--kbq-foreground-success)';
    if (tone === 'error') return 'var(--kbq-foreground-error)';
    return 'var(--kbq-foreground-contrast-secondary)';
}
