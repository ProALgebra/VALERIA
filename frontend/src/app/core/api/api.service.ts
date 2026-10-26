import { HttpClient, HttpContext, HttpContextToken, HttpHeaders, HttpInterceptorFn, HttpParams } from '@angular/common/http';
import { inject, Injectable, InjectionToken, Injector } from '@angular/core';
import { Router } from '@angular/router';
import { catchError, from, switchMap, throwError } from 'rxjs';
import { ApiError, toApiError } from './api-error';

/** CSRF token from `/api/auth/me` or `/api/auth/login`. Memory only, never persisted (SPEC §15). */
@Injectable({ providedIn: 'root' })
export class CsrfTokenStore {
    private token: string | null = null;

    get(): string | null {
        return this.token;
    }

    set(token: string | null): void {
        this.token = token;
    }
}

/** Called on any 401 (except auth probes) to drop the in-memory session. Provided by AuthService. */
export const ON_UNAUTHORIZED = new InjectionToken<() => void>('ON_UNAUTHORIZED');

/**
 * Called on any 403 (except auth probes) before the request rejects. Re-reads the session and, when the user's access
 * changed (demoted admin, «Задачи и вопросы»), re-runs the navigation so the guards show «Раздел недоступен» instead
 * of the screen (SPEC §16.2); resolves true in that case. Provided by AuthService.
 */
export const ON_FORBIDDEN = new InjectionToken<() => Promise<boolean>>('ON_FORBIDDEN');

/** Requests flagged with this token do not trigger the 401 → /login redirect (auth probes). */
export const SKIP_AUTH_REDIRECT = new HttpContextToken<boolean>(() => false);

const SAFE_METHODS = ['GET', 'HEAD', 'OPTIONS'];

/** Adds `x-csrf-token` to non-GET `/api` requests; on 401 sends the user to `/login?return=…`; 403 → ON_FORBIDDEN. */
export const apiInterceptor: HttpInterceptorFn = (req, next) => {
    const csrf = inject(CsrfTokenStore);
    const router = inject(Router);
    const injector = inject(Injector);
    let request = req;
    if (req.url.startsWith('/api/') && !SAFE_METHODS.includes(req.method)) {
        const token = csrf.get();
        if (token) request = req.clone({ setHeaders: { 'x-csrf-token': token } });
    }
    return next(request).pipe(
        catchError((error: unknown) => {
            const apiError = toApiError(error);
            if (apiError.isUnauthorized && !req.context.get(SKIP_AUTH_REDIRECT)) {
                csrf.set(null);
                injector.get(ON_UNAUTHORIZED, null)?.();
                const current = router.url;
                if (!current.startsWith('/login')) {
                    void router.navigate(['/login'], { queryParams: { return: current } });
                }
            }
            const onForbidden = apiError.isForbidden && !req.context.get(SKIP_AUTH_REDIRECT) && injector.get(ON_FORBIDDEN, null);
            if (onForbidden) {
                // The caller gets the 403 once the session is re-read; when access changed the navigation has already
                // re-run, so the screen is «Раздел недоступен» by then. Rejecting (not hanging) lets a screen that is
                // still reachable leave its loading state.
                return from(onForbidden()).pipe(
                    switchMap((changed) => {
                        // Marked so toasts and inline errors skip it: the 403 never reaches the user (SPEC §16.2).
                        if (changed) apiError.accessChanged = true;
                        return throwError(() => apiError);
                    }),
                );
            }
            return throwError(() => apiError);
        }),
    );
};

/** Key for one user action: create on click, reuse on retry after a network failure (SPEC §7). */
export function newIdempotencyKey(): string {
    if (typeof crypto !== 'undefined' && 'randomUUID' in crypto) return crypto.randomUUID();
    return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

export interface RequestOptions {
    params?: Record<string, string | number | boolean | readonly (string | number | boolean)[] | null | undefined>;
    /** Sends `Idempotency-Key`. Create it with `newIdempotencyKey()` per user action and reuse on retry. */
    idempotencyKey?: string;
    /** Don't redirect to /login on 401 (used by auth probes). */
    skipAuthRedirect?: boolean;
    /** Aborts the HTTP request; the promise rejects with `ApiError` code `aborted` (status 0). */
    signal?: AbortSignal;
}

/**
 * Thin HttpClient wrapper. All methods resolve with the parsed body or reject with `ApiError`.
 * Screens never call HttpClient directly.
 */
@Injectable({ providedIn: 'root' })
export class ApiService {
    private readonly http = inject(HttpClient);

    get<T>(url: string, options: RequestOptions = {}): Promise<T> {
        return this.request<T>('GET', url, undefined, options);
    }

    post<T>(url: string, body?: unknown, options: RequestOptions = {}): Promise<T> {
        return this.request<T>('POST', url, body ?? {}, options);
    }

    put<T>(url: string, body?: unknown, options: RequestOptions = {}): Promise<T> {
        return this.request<T>('PUT', url, body ?? {}, options);
    }

    patch<T>(url: string, body?: unknown, options: RequestOptions = {}): Promise<T> {
        return this.request<T>('PATCH', url, body ?? {}, options);
    }

    delete<T = void>(url: string, options: RequestOptions = {}): Promise<T> {
        return this.request<T>('DELETE', url, undefined, options);
    }

    private request<T>(method: string, url: string, body: unknown, options: RequestOptions): Promise<T> {
        let headers = new HttpHeaders();
        if (options.idempotencyKey) headers = headers.set('Idempotency-Key', options.idempotencyKey);
        const context = new HttpContext().set(SKIP_AUTH_REDIRECT, !!options.skipAuthRedirect);
        const { signal } = options;
        return new Promise<T>((resolve, reject) => {
            if (signal?.aborted) return reject(abortedError());
            let done = false;
            const onAbort = () => {
                subscription.unsubscribe();
                reject(abortedError());
            };
            const subscription = this.http
                .request<T>(method, url, { body, headers, params: toParams(options.params), context })
                .subscribe({
                    next: (value) => {
                        done = true;
                        signal?.removeEventListener('abort', onAbort);
                        resolve(value);
                    },
                    error: (error: unknown) => {
                        done = true;
                        signal?.removeEventListener('abort', onAbort);
                        reject(error instanceof ApiError ? error : toApiError(error));
                    },
                });
            if (!done) signal?.addEventListener('abort', onAbort, { once: true });
        });
    }
}

function abortedError(): ApiError {
    return new ApiError(0, 'aborted', null, null);
}

function toParams(params: RequestOptions['params']): HttpParams | undefined {
    if (!params) return undefined;
    let result = new HttpParams();
    for (const [key, value] of Object.entries(params)) {
        if (value === null || value === undefined || value === '') continue;
        if (Array.isArray(value)) {
            for (const v of value) result = result.append(key, String(v));
        } else {
            result = result.set(key, String(value));
        }
    }
    return result;
}
