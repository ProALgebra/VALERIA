import { newIdempotencyKey } from '../../core/api/api.service';
import { describeError } from '../../core/status/error-codes';

/** Shortcut hint shown on the second line of send tooltips (SPEC §11: `Ctrl + Enter` / `⌘ Enter`). */
export function sendShortcutHint(platform = typeof navigator === 'undefined' ? '' : navigator.platform || navigator.userAgent): string {
    return /Mac|iPhone|iPad|iPod/i.test(platform) ? '⌘ Enter' : 'Ctrl + Enter';
}

/** Ctrl/⌘ + Enter. */
export function isSendShortcut(event: KeyboardEvent): boolean {
    return event.key === 'Enter' && (event.ctrlKey || event.metaKey) && !event.isComposing;
}

export interface ComposerInput {
    /** The chat is busy (agent works, question queued…): writing is not possible now. */
    busy: boolean;
    /** Text for the disabled button while busy: «Можно будет написать, когда Valeria ответит». */
    busyReason: string;
    /** Text for the disabled button while the draft is empty: «Напишите вопрос». */
    emptyReason: string;
    draft: string;
    /** A send is in flight. */
    sending: boolean;
}

export interface ComposerState {
    /** Textarea disabled. */
    locked: boolean;
    /** Send button disabled. */
    disabled: boolean;
    /** Why the button is disabled; '' when it is enabled or sending. */
    reason: string;
}

/** Disabled-reason logic of the composer (SPEC §8.2/§8.4): busy wins over an empty draft; sending blocks quietly. */
export function composerState(input: ComposerInput): ComposerState {
    if (input.busy) return { locked: true, disabled: true, reason: input.busyReason };
    if (input.sending) return { locked: true, disabled: true, reason: '' };
    if (!input.draft.trim()) return { locked: false, disabled: true, reason: input.emptyReason };
    return { locked: false, disabled: false, reason: '' };
}

/**
 * Idempotency key per user action: the same payload retried after a failure reuses the key, a new payload gets a new
 * one, and a success forgets it.
 */
export class IdempotentAction {
    private payload: string | null = null;
    private key = '';

    keyFor(payload: unknown): string {
        const serialized = JSON.stringify(payload);
        if (serialized !== this.payload || !this.key) {
            this.payload = serialized;
            this.key = newIdempotencyKey();
        }
        return this.key;
    }

    done(): void {
        this.payload = null;
        this.key = '';
    }
}

/** «Не удалось запустить задачу: сервер недоступен» (SPEC §7). */
export function failureText(action: string, error: unknown): string {
    const { text } = describeError(error);
    const reason = text ? text.charAt(0).toLocaleLowerCase() + text.slice(1) : '';
    return reason ? `${action}: ${reason.replace(/\.$/, '')}` : action;
}
