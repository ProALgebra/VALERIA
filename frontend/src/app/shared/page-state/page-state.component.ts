import { ChangeDetectionStrategy, Component, computed, DestroyRef, effect, inject, input, output, signal } from '@angular/core';
import { KbqButtonModule } from '@koobiq/components/button';
import { KbqComponentColors } from '@koobiq/components/core';
import { KbqEmptyStateModule } from '@koobiq/components/empty-state';
import { KbqIconModule } from '@koobiq/components/icon';
import { KbqLoaderOverlayModule } from '@koobiq/components/loader-overlay';
import { isAccessChanged } from '../../core/api/api-error';
import { describeError } from '../../core/status/error-codes';

/** Loader shows only if loading lasts longer than this (SPEC §7: the shell must not blink). */
export const LOADER_DELAY_MS = 300;

/**
 * Wraps a screen's content: `kbq-loader-overlay` over the content after 300 ms of loading,
 * `kbq-empty-state` with «Попробовать снова» on error. The shell around it never changes.
 *
 * ```html
 * <v-page-state [loading]="loading()" [error]="error()" (retry)="load()">…content…</v-page-state>
 * ```
 */
@Component({
    selector: 'v-page-state',
    imports: [KbqLoaderOverlayModule, KbqEmptyStateModule, KbqButtonModule, KbqIconModule],
    changeDetection: ChangeDetectionStrategy.OnPush,
    host: { class: 'v-page-state', '[attr.aria-busy]': 'loading()' },
    template: `
        @if (shownError()) {
            <kbq-empty-state class="v-page-state__error" [errorColor]="true">
                <i kbq-empty-state-icon kbq-icon-item="kbq-triangle-exclamation_16" [big]="true" [fade]="true" [color]="colors.Contrast"></i>
                <div kbq-empty-state-title>{{ errorTitle() }}</div>
                @if (errorText(); as t) {
                    <div kbq-empty-state-text>
                        {{ t.text }}
                        @if (t.code) {
                            <div class="kbq-text-compact v-page-state__code">{{ t.code }}</div>
                        }
                    </div>
                }
                <div kbq-empty-state-actions>
                    <button kbq-button [color]="colors.Theme" kbqStyle="transparent" (click)="retry.emit()">Попробовать снова</button>
                </div>
            </kbq-empty-state>
        } @else {
            <ng-content />
        }
        @if (showLoader()) {
            <kbq-loader-overlay [transparent]="false" />
        }
    `,
    styles: `
        :host { position: relative; display: flex; flex-direction: column; flex: 1 1 auto; min-height: 0; }
        .v-page-state__error { flex: 1 1 auto; }
        .v-page-state__code { margin-top: var(--kbq-size-xs); color: var(--kbq-foreground-contrast-tertiary); }
    `,
})
export class PageStateComponent {
    readonly loading = input(false);
    readonly error = input<unknown>(null);
    /** Title of the error state; the text comes from the error itself. */
    readonly errorTitle = input('Не удалось загрузить данные');
    readonly retry = output<void>();

    protected readonly colors = KbqComponentColors;
    private readonly delayed = signal(false);
    /** A 403 that changed the user's access is never shown (SPEC §16.2): the guards already replace the screen. */
    protected readonly shownError = computed(() => (isAccessChanged(this.error()) ? null : this.error()));
    protected readonly showLoader = computed(() => this.loading() && this.delayed() && !this.shownError());
    protected readonly errorText = computed(() => (this.shownError() ? describeError(this.shownError()) : null));

    constructor() {
        let timer: ReturnType<typeof setTimeout> | null = null;
        inject(DestroyRef).onDestroy(() => timer && clearTimeout(timer));
        effect(() => {
            const loading = this.loading();
            if (timer) clearTimeout(timer);
            timer = null;
            if (!loading) {
                this.delayed.set(false);
                return;
            }
            timer = setTimeout(() => this.delayed.set(true), LOADER_DELAY_MS);
        });
    }
}
