import { signal } from '@angular/core';
import { ApiError, isAccessChanged } from '../../../core/api/api-error';
import { describeError } from '../../../core/status/error-codes';
import { Clash, readClash } from './concurrency';

export const SLOW_AFTER_MS = 30_000;
export const SLOW_TITLE = 'Операция занимает больше времени, чем ожидалось';

export type SubmitResult<T> = { ok: true; value: T } | { ok: false; cancelled: boolean; err?: unknown };

/**
 * Sync-form pattern (SPEC §15): one submit at a time, a warning after 30 s with «Подождать еще» / «Отменить»,
 * a 409/404 clash for the concurrent-edit alert, anything else as the form-level alert text.
 *
 * «Отменить» only stops waiting: the request may already be applied on the server.
 */
export class SyncSubmit {
    readonly pending = signal(false);
    readonly slow = signal(false);
    readonly error = signal<{ text: string; code: string | null } | null>(null);
    readonly clash = signal<Clash | null>(null);

    private timer: ReturnType<typeof setTimeout> | null = null;
    private cancelCurrent: (() => void) | null = null;

    constructor(private readonly slowAfterMs = SLOW_AFTER_MS) {}

    reset(): void {
        this.error.set(null);
        this.clash.set(null);
    }

    async run<T>(fn: () => Promise<T>): Promise<SubmitResult<T>> {
        if (this.pending()) return { ok: false, cancelled: true };
        this.reset();
        this.pending.set(true);
        this.armTimer();
        const cancelled = new Promise<'cancelled'>((resolve) => (this.cancelCurrent = () => resolve('cancelled')));
        try {
            const value = await Promise.race([fn(), cancelled]);
            if (value === 'cancelled') return { ok: false, cancelled: true };
            return { ok: true, value: value as T };
        } catch (err) {
            const clash = readClash(err);
            if (clash) this.clash.set(clash);
            else if (!isAccessChanged(err)) this.error.set(describeError(err));
            return { ok: false, cancelled: false, err };
        } finally {
            this.clearTimer();
            this.cancelCurrent = null;
            this.pending.set(false);
            this.slow.set(false);
        }
    }

    /** «Подождать еще»: hide the warning and give the request another interval. */
    waitMore(): void {
        this.slow.set(false);
        this.armTimer();
    }

    /** «Отменить»: stop waiting and return the form to editing. */
    cancel(): void {
        this.cancelCurrent?.();
    }

    /** Puts a server error into the form-level alert (used when a multi-request save fails midway). */
    fail(err: unknown): void {
        const clash = readClash(err);
        if (clash) this.clash.set(clash);
        else if (!isAccessChanged(err)) this.error.set(describeError(err));
    }

    private armTimer(): void {
        this.clearTimer();
        this.timer = setTimeout(() => this.slow.set(true), this.slowAfterMs);
    }

    private clearTimer(): void {
        if (this.timer) clearTimeout(this.timer);
        this.timer = null;
    }
}

/** FastAPI 422 `[{loc:["body", field], msg}]` → `{field: msg}`. */
export function fieldErrorsOf(err: unknown): Record<string, string> {
    if (!(err instanceof ApiError) || !Array.isArray(err.payload)) return {};
    const out: Record<string, string> = {};
    for (const item of err.payload as { loc?: unknown[]; msg?: string }[]) {
        const field = item.loc?.[item.loc.length - 1];
        if (typeof field === 'string' && item.msg) out[field] = item.msg;
    }
    return out;
}
