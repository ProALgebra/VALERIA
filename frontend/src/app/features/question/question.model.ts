import { plural } from '../../shared/agent-steps/steps';

/** `GET /api/consultations/:id`. */
export interface ConsultationMessage {
    id: string;
    role: 'user' | 'assistant';
    text: string;
    status?: string;
}

export interface ConsultationDetail {
    id: string;
    title: string;
    created_at: string;
    updated_at: string;
    status: string;
    messages: ConsultationMessage[];
}

/** Row of `GET /api/queue/me` (only the fields this screen reads). */
export interface QueueJob {
    id: string;
    chat_id: string;
    status: string;
    position: number | null;
    error_code: string | null;
    created_at?: string;
}

/** «В очереди, перед вами 2 задания» from the 1-based queue position; «В очереди» when nobody is ahead or unknown. */
export function queueLine(position: number | null | undefined): string {
    const ahead = typeof position === 'number' ? position - 1 : 0;
    if (ahead <= 0) return 'В очереди';
    return `В очереди, перед вами ${ahead} ${plural(ahead, 'задание', 'задания', 'заданий')}`;
}

/** Title before Valeria names the chat: a fresh question that is queued or failed shows «Новый вопрос». */
export function questionTitle(detail: ConsultationDetail | null): string {
    if (!detail) return 'Вопрос';
    const fresh = !detail.messages.some((m) => m.role === 'assistant') && detail.messages.filter((m) => m.role === 'user').length <= 1;
    if (fresh && ['queued', 'dispatching', 'failed'].includes(detail.status)) return 'Новый вопрос';
    return detail.title || 'Новый вопрос';
}
