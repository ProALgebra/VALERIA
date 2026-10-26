import { ChangeDetectionStrategy, Component, inject, input } from '@angular/core';
import { KbqButtonModule } from '@koobiq/components/button';
import { KbqComponentColors } from '@koobiq/components/core';
import { KbqIconModule } from '@koobiq/components/icon';
import { KbqProgressSpinnerModule } from '@koobiq/components/progress-spinner';
import { NotifyService } from '../../core/notify/notify.service';

/** Clock time of a chat message: «14:16». */
export function messageTime(value: string | number | null | undefined): string {
    if (value === null || value === undefined || value === '') return '';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return '';
    return date.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
}

/** The user's own message: right-aligned bubble and time (prototype `.v-msg--me`). */
@Component({
    selector: 'v-user-message',
    changeDetection: ChangeDetectionStrategy.OnPush,
    template: `
        <div class="v-msg__bubble">{{ text() }}</div>
        @if (time()) {
            <div class="v-msg__meta kbq-text-compact">{{ time() }}</div>
        }
    `,
    styles: `
        :host { display: flex; flex-direction: column; align-items: flex-end; margin: var(--kbq-size-l) 0; }
        .v-msg__bubble {
            max-width: 80%;
            box-sizing: border-box;
            padding: var(--kbq-size-s) var(--kbq-size-m);
            border-radius: var(--kbq-size-m) var(--kbq-size-m) var(--kbq-size-xxs) var(--kbq-size-m);
            background: var(--kbq-background-theme-less);
            white-space: pre-wrap;
            overflow-wrap: anywhere;
        }
        .v-msg__meta {
            margin-top: var(--kbq-size-xxs);
            color: var(--kbq-foreground-contrast-tertiary);
        }
    `,
})
export class UserMessageComponent {
    readonly text = input.required<string>();
    readonly time = input('');
}

/**
 * Valeria's message (prototype `.v-msg`): «Valeria» with the logo, projected content, then time and an optional
 * «Копировать ответ» that copies `copyText`.
 */
@Component({
    selector: 'v-bot-message',
    imports: [KbqButtonModule, KbqIconModule],
    changeDetection: ChangeDetectionStrategy.OnPush,
    template: `
        <div class="v-msg__who"><span class="v-logo" aria-hidden="true"></span>Valeria</div>
        <ng-content />
        @if (time() || copyText()) {
            <div class="v-msg__actions">
                @if (time()) {
                    <span class="v-msg__time kbq-text-compact">{{ time() }}</span>
                }
                @if (copyText()) {
                    <button kbq-button kbqStyle="transparent" [color]="colors.Contrast" (click)="copy()">
                        <i kbq-icon="kbq-square-multiple-o_16"></i>
                        Копировать ответ
                    </button>
                }
            </div>
        }
    `,
    styles: `
        :host { display: block; margin: var(--kbq-size-l) 0; }
        .v-msg__who {
            display: flex;
            align-items: center;
            gap: var(--kbq-size-xs);
            margin-bottom: var(--kbq-size-xs);
            font-weight: var(--kbq-typography-text-normal-strong-font-weight);
        }
        .v-logo {
            display: inline-block;
            flex: none;
            width: var(--kbq-size-xl);
            height: var(--kbq-size-xl);
            border-radius: var(--kbq-size-xs);
            background: var(--kbq-background-theme);
        }
        .v-msg__actions {
            display: flex;
            align-items: center;
            gap: var(--kbq-size-m);
            margin-top: var(--kbq-size-s);
        }
        .v-msg__time { color: var(--kbq-foreground-contrast-tertiary); }
    `,
})
export class BotMessageComponent {
    readonly time = input('');
    /** Text put on the clipboard by «Копировать ответ»; no button when empty. */
    readonly copyText = input('');

    private readonly notify = inject(NotifyService);
    protected readonly colors = KbqComponentColors;

    protected async copy(): Promise<void> {
        try {
            await navigator.clipboard.writeText(this.copyText());
            this.notify.info('Скопировано');
        } catch {
            this.notify.error('Не удалось скопировать ответ');
        }
    }
}

/** Progress line with a spinner (prototype `.v-status`): «Valeria готовит ответ / Обычно 1–3 минуты.». */
@Component({
    selector: 'v-status-line',
    imports: [KbqProgressSpinnerModule],
    changeDetection: ChangeDetectionStrategy.OnPush,
    host: { role: 'status' },
    template: `
        <kbq-progress-spinner mode="indeterminate" class="v-status__spinner" />
        <div>
            <div>{{ text() }}</div>
            @if (sub()) {
                <div class="v-status__sub">{{ sub() }}</div>
            }
        </div>
    `,
    styles: `
        :host { display: flex; gap: var(--kbq-size-m); align-items: flex-start; padding: var(--kbq-size-s) 0; color: var(--kbq-foreground-contrast); }
        .v-status__spinner { flex: none; margin-top: var(--kbq-size-3xs); }
        .v-status__sub { color: var(--kbq-foreground-contrast-secondary); }
    `,
})
export class StatusLineComponent {
    readonly text = input.required<string>();
    readonly sub = input('');
}
