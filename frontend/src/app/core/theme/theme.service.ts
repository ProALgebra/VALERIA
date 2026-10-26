import { DestroyRef, inject, Injectable, signal } from '@angular/core';
import { KbqDefaultThemes, ThemeService } from '@koobiq/components/core';
import { UI_KEYS, UiStateService } from '../state/ui-state.service';

export type ThemeMode = 'system' | 'light' | 'dark';

/** Single-line options of the theme menu. */
export const THEME_OPTIONS: readonly { id: ThemeMode; label: string }[] = [
    { id: 'system', label: 'Системная' },
    { id: 'light', label: 'Светлая' },
    { id: 'dark', label: 'Тёмная' },
];

/**
 * Light / dark / system theme through Koobiq `ThemeService` (body class `kbq-light` / `kbq-dark`).
 * The choice lives in `valeria.theme`, default «Системная».
 */
@Injectable({ providedIn: 'root' })
export class AppThemeService {
    private readonly kbqTheme = inject(ThemeService);
    private readonly mode$ = inject(UiStateService).state(UI_KEYS.theme);
    private readonly media =
        typeof window !== 'undefined' && window.matchMedia ? window.matchMedia('(prefers-color-scheme: dark)') : null;
    private readonly _effective = signal<'light' | 'dark'>('light');

    readonly mode = this.mode$;
    /** The theme actually shown. */
    readonly effective = this._effective.asReadonly();

    constructor() {
        this.kbqTheme.setThemes(KbqDefaultThemes.map((t) => ({ ...t })));
        const onChange = () => this.apply();
        this.media?.addEventListener('change', onChange);
        inject(DestroyRef).onDestroy(() => this.media?.removeEventListener('change', onChange));
    }

    init(): void {
        this.apply();
    }

    setMode(mode: ThemeMode): void {
        this.mode$.set(mode);
        this.apply();
    }

    private apply(): void {
        const mode = this.mode$();
        const dark = mode === 'dark' || (mode === 'system' && !!this.media?.matches);
        const next = dark ? 'dark' : 'light';
        const current = this.kbqTheme.getTheme();
        if (current && current.name === next) return;
        this._effective.set(next);
        this.kbqTheme.setTheme(dark ? 1 : 0);
    }
}
