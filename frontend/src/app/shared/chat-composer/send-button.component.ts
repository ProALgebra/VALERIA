import { ChangeDetectionStrategy, Component, input, output } from '@angular/core';
import { KbqButtonModule, KbqButtonStyles } from '@koobiq/components/button';
import { KbqComponentColors, PopUpPlacements } from '@koobiq/components/core';
import { KbqIconModule } from '@koobiq/components/icon';
import { KbqToolTipModule } from '@koobiq/components/tooltip';
import { sendShortcutHint } from './send-action';

/**
 * Send button of the async-action pattern: disabled with `kbq-progress` while the request is in flight; the tooltip
 * shows the disabled reason, or the label with the Ctrl/⌘ Enter hint on the second line when `shortcut` is on.
 * A disabled button can't be hovered or focused, so the tooltip sits on a wrapper that becomes focusable instead.
 */
@Component({
    selector: 'v-send-button',
    imports: [KbqButtonModule, KbqIconModule, KbqToolTipModule],
    changeDetection: ChangeDetectionStrategy.OnPush,
    template: `
        <span
            class="v-send-button__wrap"
            [kbqTooltip]="tip"
            [kbqTooltipDisabled]="!reason() && !shortcut()"
            [kbqPlacement]="placement()"
            [attr.tabindex]="disabled() && reason() ? 0 : null"
            [attr.aria-label]="disabled() && reason() ? label() + '. ' + reason() : null"
        >
            <button
                kbq-button
                type="button"
                [color]="color()"
                [kbqStyle]="kbqStyle()"
                [disabled]="disabled() || sending()"
                [class.kbq-progress]="sending()"
                [attr.aria-busy]="sending()"
                (click)="pressed.emit()"
            >
                <!-- The icon is declared here, not projected: Koobiq finds button icons with a content query, which
                     only sees this template, and then sets the stock icon/text gap classes. -->
                @if (icon(); as name) {
                    <i [kbq-icon]="name"></i>
                }
                <ng-content />
            </button>
        </span>
        <ng-template #tip>
            @if (reason()) {
                <div>{{ reason() }}</div>
            } @else {
                <div>{{ label() }}</div>
                <div class="v-send-button__hint">{{ hint }}</div>
            }
        </ng-template>
    `,
    styles: [
        `
        :host { display: inline-flex; }
        .v-send-button__wrap { display: inline-flex; border-radius: var(--kbq-size-border-radius); }
        .v-send-button__wrap:focus-visible { outline: 2px solid var(--kbq-states-line-focus-theme); outline-offset: 1px; }
        .v-send-button__hint { opacity: 0.72; }
    `,
    ],
})
export class SendButtonComponent {
    readonly label = input.required<string>();
    /** Koobiq icon name shown before the text (e.g. `kbq-play_16`). */
    readonly icon = input('');
    readonly disabled = input(false);
    readonly sending = input(false);
    /** Why the button is disabled; shown as the tooltip. */
    readonly reason = input('');
    /** Show «⌘ Enter» on the second tooltip line. */
    readonly shortcut = input(true);
    readonly color = input<KbqComponentColors>(KbqComponentColors.Contrast);
    readonly kbqStyle = input<KbqButtonStyles | string>(KbqButtonStyles.Filled);
    readonly placement = input<PopUpPlacements>(PopUpPlacements.Top);
    readonly pressed = output<void>();

    protected readonly hint = sendShortcutHint();
}
