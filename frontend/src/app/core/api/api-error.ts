import { HttpErrorResponse } from '@angular/common/http';

/**
 * Normalized API error. Backend errors come as `{ detail: "text" }`, `{ detail: { code, … } }`
 * or FastAPI validation `{ detail: [{ loc, msg }] }`.
 */
export class ApiError extends Error {
    constructor(
        /** HTTP status; 0 = network failure (no response). */
        readonly status: number,
        /** `detail.code` when the backend sent a structured error. */
        readonly code: string | null,
        /** Human text from `detail` (string or `detail.message`/`detail.detail`), if any. */
        readonly detail: string | null,
        /** Raw `detail` payload for screens that need extra fields. */
        readonly payload: unknown,
    ) {
        super(detail || code || (status ? `HTTP ${status}` : 'Network error'));
        this.name = 'ApiError';
    }

    /**
     * Set by the API interceptor on a 403 that changed the user's access: the screen is already «Раздел недоступен»
     * and the error is never shown, neither as a toast nor inline (SPEC §16.2). Check with `isAccessChanged`.
     */
    accessChanged = false;

    get isNetwork(): boolean {
        return this.status === 0;
    }

    get isUnauthorized(): boolean {
        return this.status === 401;
    }

    get isForbidden(): boolean {
        return this.status === 403;
    }

    get isNotFound(): boolean {
        return this.status === 404;
    }

    get isConflict(): boolean {
        return this.status === 409;
    }
}

/** True for a 403 after which the user's access changed: show nothing for it (SPEC §16.2). */
export function isAccessChanged(error: unknown): boolean {
    return error instanceof ApiError && error.accessChanged;
}

export function toApiError(error: unknown): ApiError {
    if (error instanceof ApiError) return error;
    if (error instanceof HttpErrorResponse) {
        const body = error.error as unknown;
        const raw = body && typeof body === 'object' && 'detail' in body ? (body as { detail: unknown }).detail : body;
        return new ApiError(error.status, pickCode(raw), pickText(raw), raw ?? null);
    }
    return new ApiError(0, null, null, null);
}

function pickCode(raw: unknown): string | null {
    if (raw && typeof raw === 'object' && !Array.isArray(raw)) {
        const code = (raw as Record<string, unknown>)['code'];
        return typeof code === 'string' ? code : null;
    }
    return null;
}

function pickText(raw: unknown): string | null {
    if (typeof raw === 'string') return raw || null;
    if (Array.isArray(raw)) {
        const msgs = raw
            .map((x) => (x && typeof x === 'object' ? (x as Record<string, unknown>)['msg'] : null))
            .filter((m): m is string => typeof m === 'string');
        return msgs.length ? msgs.join('; ') : null;
    }
    if (raw && typeof raw === 'object') {
        const obj = raw as Record<string, unknown>;
        for (const k of ['message', 'detail', 'error']) {
            if (typeof obj[k] === 'string' && obj[k]) return obj[k] as string;
        }
    }
    return null;
}
