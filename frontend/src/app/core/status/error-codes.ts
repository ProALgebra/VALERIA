import { ApiError } from '../api/api-error';

/**
 * `error_code` → text for the error alert (SPEC §9). Unknown code → «Внутренняя ошибка» and the code in small print.
 *
 * NOTE: the prototype has no texts for these codes; the texts below are drafts written for the foundation
 * and must be reviewed by the product owner.
 */
const TEXTS: Record<string, string> = {
    opencode_unavailable: 'OpenCode сейчас недоступен. Попробуйте позже.',
    interrupted_by_restart: 'Работу прервал перезапуск сервера.',
    recovery_unavailable: 'После перезапуска сервера работу не удалось восстановить.',
    assistant_error: 'Valeria завершила работу с ошибкой.',
    incomplete_response: 'Ответ пришёл не полностью.',
    build_submission_uncertain: 'Не удалось подтвердить, что задача принята в работу.',
    internal_error: 'Внутренняя ошибка',
    aborted: 'Работа прервана.',
    cancelled: 'Работа отменена.',
    stop_unconfirmed: 'Остановка пока не подтверждена. Повторяем автоматически.',
};

export const NETWORK_ERROR_TEXT = 'Сервер недоступен';
export const UNKNOWN_ERROR_TEXT = 'Внутренняя ошибка';

export interface ErrorCodeText {
    text: string;
    /** Set for unknown codes: show it in small print under the text. */
    code: string | null;
}

export function errorCodeText(code: string | null | undefined): ErrorCodeText {
    if (code && TEXTS[code]) return { text: TEXTS[code], code: null };
    return { text: UNKNOWN_ERROR_TEXT, code: code || null };
}

/** Text for any thrown error: known code → its text; network → «Сервер недоступен»; backend detail; else unknown. */
export function describeError(err: unknown): ErrorCodeText {
    if (err instanceof ApiError) {
        if (err.code) return errorCodeText(err.code);
        if (err.isNetwork) return { text: NETWORK_ERROR_TEXT, code: null };
        if (err.detail) return { text: err.detail, code: null };
        return errorCodeText(err.status ? `HTTP ${err.status}` : null);
    }
    return errorCodeText(null);
}
