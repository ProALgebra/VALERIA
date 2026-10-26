import { ChangeDetectionStrategy, Component, ElementRef, inject, input, signal } from '@angular/core';

/**
 * One bordered box around the message field and the composer bar (prototype `.v-inputbox`): a borderless
 * `kbq-form-field` with the textarea on top, the controls and the send button in a row below it inside the same
 * frame, so text never runs under them. The frame reuses the stock form-field tokens (border, focus ring, disabled).
 *
 * Slots: `.v-composer-box__field` (a `kbq-form-field noBorders` with the textarea), `v-send-button`; everything else
 * goes to the bottom-left controls (project one element that lays out its own row, e.g. a wrapping flex bar).
 * The send button comes first in the DOM so the tab order stays textarea → send → controls, as before.
 */
@Component({
    selector: 'v-composer-box',
    changeDetection: ChangeDetectionStrategy.OnPush,
    host: {
        class: 'v-composer-box',
        '[class.v-composer-box_focused]': 'focused()',
        '[class.v-composer-box_disabled]': 'disabled()',
        '(focusin)': 'onFocus($event)',
        '(focusout)': 'onFocus($event)',
    },
    template: `
        <ng-content select=".v-composer-box__field" />
        <div class="v-composer-box__bar" (click)="onBarClick($event)">
            <ng-content select="v-send-button" />
            <div class="v-composer-box__controls"><ng-content /></div>
        </div>
    `,
    styles: `
        :host {
            position: relative;
            isolation: isolate;
            display: block;
            box-sizing: border-box;
            border: var(--kbq-size-border-width) solid var(--kbq-line-contrast-fade);
            border-radius: var(--kbq-size-border-radius);
            background: var(--kbq-background-bg);
        }
        :host(.v-composer-box_focused) { border-color: var(--kbq-states-line-focus-theme); }
        /* The inner line of the ring (the stock form-field focus shadow) is drawn on a layer above the content:
           as an inset shadow of the host it would sit under the field's own background and show only along the bar. */
        :host(.v-composer-box_focused)::after {
            content: '';
            position: absolute;
            inset: 0;
            z-index: 1;
            border-radius: calc(var(--kbq-size-border-radius) - var(--kbq-size-border-width));
            box-shadow: inset 0 0 0.1px 1px var(--kbq-states-line-focus-theme);
            pointer-events: none;
        }
        /* The disabled field tints itself (translucent), so only the bar gets the same tint here, not the whole box. */
        :host(.v-composer-box_disabled) { border-color: var(--kbq-states-line-disabled); }
        :host(.v-composer-box_disabled) .v-composer-box__bar {
            background: var(--kbq-states-background-disabled);
            border-radius: 0 0 var(--kbq-size-border-radius) var(--kbq-size-border-radius);
        }
        .v-composer-box__bar {
            display: flex;
            flex-wrap: wrap;
            align-items: flex-end;
            justify-content: flex-end;
            gap: var(--kbq-size-s);
            padding: 0 var(--kbq-size-xs) var(--kbq-size-xs);
        }
        /* Send keeps the row while the controls fit next to it at their narrowest, then drops to its own line;
           alone on a line, the controls shrink to the box. The projected bar brings its own wrapping row. */
        .v-composer-box__controls {
            order: -1;
            flex: 1 1 min-content;
            min-width: 0;
        }
        .v-composer-box__controls:empty { display: none; }
    `,
})
export class ComposerBoxComponent {
    /** Paint the frame disabled, in step with the textarea. */
    readonly disabled = input(false);

    private readonly host = inject<ElementRef<HTMLElement>>(ElementRef);
    protected readonly focused = signal(false);

    /** The focus ring follows the textarea only, like a stock form field; the bar controls have their own. */
    protected onFocus(event: FocusEvent): void {
        this.focused.set(event.type === 'focusin' && event.target instanceof HTMLTextAreaElement);
    }

    /** A click on the empty part of the bar focuses the textarea, as a click on a form field's padding does. */
    protected onBarClick(event: MouseEvent): void {
        const target = event.target as Element;
        if (target.closest('button, a, kbq-form-field, [tabindex]')) return;
        this.host.nativeElement.querySelector('textarea')?.focus();
    }
}
