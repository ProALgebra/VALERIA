import { afterNextRender, ChangeDetectionStrategy, Component, computed, ElementRef, inject, input, output } from '@angular/core';
import { KbqAlertModule, KbqAlertColors, KbqAlertStyles } from '@koobiq/components/alert';
import { KbqIconModule } from '@koobiq/components/icon';
import { KbqLinkModule } from '@koobiq/components/link';
import { clashTitle } from './concurrency';
import { SLOW_TITLE, SyncSubmit } from './sync-submit';

/**
 * Entity form page (prototype `formPage`): sections with labels on the left and a footer that follows the form
 * and sticks to the bottom when the form is taller than the screen. No crumbs: leave via «Сохранить» or «Отмена».
 *
 * ```html
 * <v-form-page>
 *   <fieldset vFsec="Доступ">…</fieldset>
 *   <ng-container vFootMain>…</ng-container>
 *   <ng-container vFootExtra>…</ng-container>
 * </v-form-page>
 * ```
 * The first field of the form gets the focus when the page opens (see {@link focusFirstField}).
 */
@Component({
    selector: 'v-form-page',
    changeDetection: ChangeDetectionStrategy.OnPush,
    host: { '[class.v-form-page_wide]': 'wide()' },
    template: `
        <div class="v-form"><ng-content /></div>
        <div class="v-formfoot">
            <div class="v-formfoot__main"><ng-content select="[vFootMain]" /></div>
            <div class="v-formfoot__extra"><ng-content select="[vFootExtra]" /></div>
        </div>
    `,
    styles: `
        :host {
            display: flex;
            flex-direction: column;
            flex: 1 1 auto;
            min-height: 0;
            overflow: auto;
            padding: var(--kbq-size-l) var(--kbq-size-xxl) 0;
        }
        .v-form {
            max-width: 880px;
            display: flex;
            flex-direction: column;
            gap: var(--kbq-size-3xl);
            padding-bottom: var(--kbq-size-s);
            margin-bottom: var(--kbq-size-xxl);
        }
        /* Wide form (queue settings: the queue table needs the room). */
        :host(.v-form-page_wide) .v-form { max-width: 960px; }
        .v-formfoot {
            position: sticky;
            bottom: 0;
            z-index: 5;
            margin: auto calc(-1 * var(--kbq-size-xxl)) 0;
            padding: var(--kbq-size-m) var(--kbq-size-xxl);
            display: flex;
            align-items: center;
            gap: var(--kbq-size-s);
            background: var(--kbq-background-bg);
            border-top: 1px solid var(--kbq-line-contrast-less);
            box-shadow: var(--kbq-shadow-card);
        }
        .v-formfoot__main { display: flex; gap: var(--kbq-size-s); }
        .v-formfoot__extra { margin-left: auto; display: flex; gap: var(--kbq-size-s); }
        .v-formfoot__extra:empty { display: none; }
    `,
})
export class FormPageComponent {
    /** 960px instead of 880px. */
    readonly wide = input(false);

    constructor() {
        const host: HTMLElement = inject(ElementRef).nativeElement;
        afterNextRender(() => focusFirstField(host));
    }
}

/**
 * The first control in the form sections: a text field, a select, a toggle, or the checked option of a radio group.
 * Alerts above the sections and the footer are skipped.
 */
const FIRST_FIELD = [
    'fieldset input:not([type="radio"]):not(:disabled)',
    'fieldset input[type="radio"]:checked:not(:disabled)',
    'fieldset textarea:not(:disabled)',
    'fieldset kbq-select:not(.kbq-disabled)',
].join(', ');

/** Focuses the first field of a form page; does nothing when the form has none. */
export function focusFirstField(root: HTMLElement): void {
    root.querySelector<HTMLElement>(FIRST_FIELD)?.focus();
}

/** Form section (prototype `fsec`): `<fieldset vFsec="Исполнение">`. */
@Component({
    selector: 'fieldset[vFsec]',
    changeDetection: ChangeDetectionStrategy.OnPush,
    template: `<legend class="v-fsec__title">{{ vFsec() }}</legend><ng-content />`,
    styles: `
        :host { position: relative; border: 0; padding: 0; margin: 0; display: flex; flex-direction: column; gap: var(--kbq-size-l); min-width: 0; }
        .v-fsec__title {
            padding: 0;
            margin-bottom: var(--kbq-size-xxs);
            font-family: var(--kbq-typography-text-big-strong-font-family);
            font-size: var(--kbq-typography-text-big-strong-font-size);
            line-height: var(--kbq-typography-text-big-strong-line-height);
            font-weight: var(--kbq-typography-text-big-strong-font-weight);
            letter-spacing: var(--kbq-typography-text-big-strong-letter-spacing);
        }
    `,
})
export class FsecComponent {
    readonly vFsec = input.required<string>();
}

/** Form row (prototype `frow`): label column 200px, control column. `for` links the label to the control. */
@Component({
    selector: 'v-frow',
    changeDetection: ChangeDetectionStrategy.OnPush,
    template: `
        @if (label()) {
            <label class="v-frow__label" [attr.for]="for() || null" [attr.id]="labelId() || null">
                {{ label() }}@if (required()) {
                    <span class="v-frow__req" aria-hidden="true"> *</span>
                }
            </label>
        } @else {
            <span></span>
        }
        <div class="v-frow__ctl"><ng-content /></div>
    `,
    styles: `
        :host { display: grid; grid-template-columns: 200px minmax(0, 1fr); gap: var(--kbq-size-l); align-items: start; }
        .v-frow__label { padding-top: var(--kbq-size-xs); color: var(--kbq-foreground-contrast-secondary); }
        .v-frow__req { color: var(--kbq-foreground-error); }
        .v-frow__ctl { display: flex; flex-direction: column; gap: var(--kbq-size-xxs); min-width: 0; }
    `,
})
export class FrowComponent {
    readonly label = input('');
    readonly required = input(false);
    readonly for = input('');
    /** For `aria-labelledby` on a group control (radio group, checkbox list). */
    readonly labelId = input('');
}

/**
 * Alerts above the form: slow request (warning, «Подождать еще» / «Отменить»), concurrent change (warning,
 * «Перезаписать»), deleted object (error) and any other server error (error).
 */
@Component({
    selector: 'v-form-alerts',
    imports: [KbqAlertModule, KbqIconModule, KbqLinkModule],
    changeDetection: ChangeDetectionStrategy.OnPush,
    template: `
        @if (submit().slow()) {
            <kbq-alert [compact]="true" [alertColor]="colors.Warning" [alertStyle]="styles.Colored" role="status">
                <i kbq-icon="kbq-triangle-exclamation_16" color="warning"></i>
                {{ slowTitle }}
                <button type="button" kbq-alert-control kbq-link pseudo (click)="submit().waitMore()">Подождать еще</button>
                <button type="button" kbq-alert-control kbq-link pseudo (click)="submit().cancel()">Отменить</button>
            </kbq-alert>
        }
        @if (submit().clash(); as clash) {
            @if (clash.kind === 'conflict') {
                <kbq-alert [compact]="true" [alertColor]="colors.Warning" [alertStyle]="styles.Colored" role="alert">
                    <i kbq-icon="kbq-triangle-exclamation_16" color="warning"></i>
                    {{ title(clash) }}
                    <button type="button" kbq-alert-control kbq-link pseudo (click)="overwrite.emit()">Перезаписать</button>
                </kbq-alert>
            } @else {
                <kbq-alert [compact]="true" [alertColor]="colors.Error" [alertStyle]="styles.Colored" role="alert">
                    <i kbq-icon="kbq-circle-exclamation_16" color="error"></i>
                    {{ title(clash) }}
                    @if (recreatable()) {
                        <button type="button" kbq-alert-control kbq-link pseudo (click)="recreate.emit()">Создать объект</button>
                    }
                </kbq-alert>
            }
        }
        @if (submit().error(); as err) {
            <kbq-alert [compact]="true" [alertColor]="colors.Error" [alertStyle]="styles.Colored" role="alert">
                <i kbq-icon="kbq-circle-exclamation_16" color="error"></i>
                <div kbq-alert-title>Не удалось сохранить</div>
                {{ err.text }}
                @if (err.code) {
                    <span class="v-form-alerts__code">{{ err.code }}</span>
                }
            </kbq-alert>
        }
    `,
    host: { '[style.display]': 'shown() ? null : "none"' },
    styles: `
        :host { display: flex; flex-direction: column; gap: var(--kbq-size-m); }
        .v-form-alerts__code { display: block; color: var(--kbq-foreground-contrast-tertiary); }
    `,
})
export class FormAlertsComponent {
    readonly submit = input.required<SyncSubmit>();
    readonly users = input<readonly { id: string; login: string }[]>([]);
    readonly recreatable = input(false);
    readonly overwrite = output<void>();
    readonly recreate = output<void>();

    protected readonly colors = KbqAlertColors;
    protected readonly styles = KbqAlertStyles;
    protected readonly slowTitle = SLOW_TITLE;
    protected readonly shown = computed(() => {
        const s = this.submit();
        return s.slow() || !!s.clash() || !!s.error();
    });

    protected title(clash: Parameters<typeof clashTitle>[0]): string {
        return clashTitle(clash, this.users());
    }
}
