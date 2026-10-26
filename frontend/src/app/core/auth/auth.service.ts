import { computed, inject, Injectable, signal } from '@angular/core';
import { Router } from '@angular/router';
import { ApiError } from '../api/api-error';
import { ApiService, CsrfTokenStore } from '../api/api.service';

export type UserRole = 'user' | 'admin';

/** `public_user` from the backend. */
export interface User {
    id: number;
    login: string;
    role: UserRole | string;
    /** «Задачи и вопросы»: runs tasks and asks questions, no folder work, no /api/chats*. */
    tasks_only: boolean;
    workspace_path: string | null;
    mattermost_username?: string | null;
    mattermost_user_id?: string | null;
    status?: string;
    max_active_jobs?: number | null;
    created_at?: string;
    updated_at?: string;
    last_login_at?: string | null;
}

interface AuthResponse {
    user: User;
    csrf_token: string;
}

/** Role caption for the user block: `login · роль`. */
export function roleLabel(user: Pick<User, 'role' | 'tasks_only'>): string {
    if (user.role === 'admin') return 'Администратор';
    if (user.tasks_only) return 'Задачи и вопросы';
    return 'Пользователь';
}

@Injectable({ providedIn: 'root' })
export class AuthService {
    private readonly api = inject(ApiService);
    private readonly csrf = inject(CsrfTokenStore);
    private readonly router = inject(Router);

    private readonly _user = signal<User | null>(null);
    private readonly _loadError = signal<unknown>(null);
    private loading: Promise<User | null> | null = null;
    private forbidden: Promise<boolean> | null = null;
    private loaded = false;

    /** Current user or null when signed out / not loaded yet. */
    readonly user = this._user.asReadonly();
    /** Last non-401 failure of `/api/auth/me` (network, 5xx); the app shows an error state with retry. */
    readonly loadError = this._loadError.asReadonly();
    readonly isAdmin = computed(() => this._user()?.role === 'admin');
    readonly tasksOnly = computed(() => !!this._user()?.tasks_only && this._user()?.role !== 'admin');
    readonly roleLabel = computed(() => {
        const u = this._user();
        return u ? roleLabel(u) : '';
    });

    /** Loads `/api/auth/me` once; later calls reuse the result. Resolves null when not signed in. */
    ensureLoaded(): Promise<User | null> {
        if (this.loaded) return Promise.resolve(this._user());
        if (!this.loading) {
            this.loading = this.api
                .get<AuthResponse>('/api/auth/me', { skipAuthRedirect: true })
                .then((res) => this.accept(res))
                .catch((error: unknown) => {
                    if (error instanceof ApiError && error.isUnauthorized) {
                        this.reset();
                        return null;
                    }
                    this._loadError.set(error);
                    throw error;
                })
                .finally(() => {
                    this.loading = null;
                });
        }
        return this.loading;
    }

    /** Re-reads the session (e.g. after the backend changed the user). */
    refresh(): Promise<User | null> {
        this.loaded = false;
        return this.ensureLoaded();
    }

    /**
     * After a 403: re-reads the session and, when the role or «Задачи и вопросы» changed, re-runs the current
     * navigation so the route guards show «Раздел недоступен» (SPEC §16.2). Resolves true when access changed.
     */
    handleForbidden(): Promise<boolean> {
        this.forbidden ??= (async () => {
            const before = this._user();
            try {
                const after = await this.refresh();
                const changed = !after || after.role !== before?.role || !!after.tasks_only !== !!before?.tasks_only;
                if (changed) await this.router.navigateByUrl(this.router.url, { onSameUrlNavigation: 'reload' });
                return changed;
            } catch {
                return false;
            } finally {
                this.forbidden = null;
            }
        })();
        return this.forbidden;
    }

    /** Rejects with ApiError; 401 detail is «Неверный логин или пароль». */
    async login(login: string, password: string): Promise<User> {
        const res = await this.api.post<AuthResponse>('/api/auth/login', { login, password }, { skipAuthRedirect: true });
        return this.accept(res);
    }

    async logout(): Promise<void> {
        try {
            await this.api.post<void>('/api/auth/logout', {}, { skipAuthRedirect: true });
        } catch {
            /* the session is gone either way */
        }
        this.reset();
        await this.router.navigateByUrl('/login');
    }

    /** Clears the in-memory session after a 401. */
    reset(): void {
        this._loadError.set(null);
        this._user.set(null);
        this.csrf.set(null);
        this.loaded = true;
    }

    private accept(res: AuthResponse): User {
        this._loadError.set(null);
        this.csrf.set(res.csrf_token);
        this._user.set(res.user);
        this.loaded = true;
        return res.user;
    }
}
