import { ChangeDetectionStrategy, Component, computed, effect, input, signal, untracked } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { KbqCheckboxModule } from '@koobiq/components/checkbox';
import { KbqComponentColors } from '@koobiq/components/core';
import { KbqFormFieldModule } from '@koobiq/components/form-field';
import { KbqIconModule } from '@koobiq/components/icon';
import { KbqRadioModule } from '@koobiq/components/radio';
import { KbqTextareaModule } from '@koobiq/components/textarea';
import { SendButtonComponent } from '../chat-composer/send-button.component';
import { isSendShortcut } from '../chat-composer/send-action';
import { allowsCustom, buildReplyPayload, emptySelection, QuestionRequest, QuestionSelection } from './clarify.model';

const CUSTOM = '\u0000custom';

/**
 * «Valeria уточняет» card (prototype `.v-qcard`): radios for single choice, checkboxes for multiple, «Свой ответ» with
 * a textarea. `submit` sends `answers: string[][]` and resolves `true` on success.
 */
@Component({
    selector: 'v-clarify-card',
    imports: [FormsModule, KbqRadioModule, KbqCheckboxModule, KbqFormFieldModule, KbqTextareaModule, KbqIconModule, SendButtonComponent],
    changeDetection: ChangeDetectionStrategy.OnPush,
    host: { role: 'group', 'aria-label': 'Valeria уточняет', '(keydown)': 'onKeydown($event)' },
    template: `
        <div class="v-qcard__head">
            <i kbq-icon="kbq-circle-question_16" [color]="colors.Theme"></i>
            Valeria уточняет
        </div>
        @for (q of request().questions; track $index; let i = $index) {
            <p class="v-qcard__question">{{ q.question }}</p>
            <div class="v-qcard__options">
                @if (!q.multiple) {
                    <kbq-radio-group
                        [name]="request().id + '-' + i"
                        [value]="radioValue(i)"
                        [attr.aria-label]="q.question"
                        (change)="pickRadio(i, $event.value)"
                    >
                        @for (o of q.options; track o.label) {
                            <kbq-radio-button [value]="o.label">
                                {{ o.label }}
                                @if (o.description) {
                                    <kbq-hint>{{ o.description }}</kbq-hint>
                                }
                            </kbq-radio-button>
                        }
                        @if (custom(q)) {
                            <kbq-radio-button [value]="customValue">Свой ответ</kbq-radio-button>
                        }
                    </kbq-radio-group>
                } @else {
                    @for (o of q.options; track o.label) {
                        <kbq-checkbox [checked]="isChecked(i, o.label)" (change)="toggle(i, o.label, $event.checked)">
                            {{ o.label }}
                            @if (o.description) {
                                <kbq-hint>{{ o.description }}</kbq-hint>
                            }
                        </kbq-checkbox>
                    }
                    @if (custom(q)) {
                        <kbq-checkbox [checked]="!!selections()[i]?.customOn" (change)="setCustom(i, $event.checked)">Свой ответ</kbq-checkbox>
                    }
                }
                @if (selections()[i]?.customOn && custom(q)) {
                    <kbq-form-field class="v-qcard__custom">
                        <textarea
                            kbqTextarea
                            [canGrow]="false"
                            rows="2"
                            placeholder="Опишите своими словами"
                            aria-label="Свой ответ"
                            [ngModel]="selections()[i].customText"
                            (ngModelChange)="setCustomText(i, $event)"
                        ></textarea>
                    </kbq-form-field>
                }
            </div>
        }
        <div class="v-qcard__actions">
            <v-send-button
                label="Отправить ответ"
                [disabled]="!payload()"
                [sending]="sending()"
                [reason]="payload() ? '' : 'Выберите вариант или напишите свой ответ'"
                (pressed)="send()"
                >Отправить ответ</v-send-button
            >
        </div>
    `,
    styles: `
        :host {
            display: block;
            box-sizing: border-box;
            padding: var(--kbq-size-l);
            border: 1px solid var(--kbq-line-theme-fade);
            border-radius: var(--kbq-size-m);
            background: var(--kbq-background-bg);
        }
        .v-qcard__head { display: flex; align-items: center; gap: var(--kbq-size-s); font-weight: var(--kbq-typography-text-normal-strong-font-weight); }
        .v-qcard__question { margin: 0; }
        .v-qcard__options { display: flex; flex-direction: column; gap: var(--kbq-size-s); margin-top: var(--kbq-size-s); }
        .v-qcard__options kbq-radio-group { display: flex; flex-direction: column; gap: var(--kbq-size-s); }
        .v-qcard__custom { display: block; width: 100%; }
        .v-qcard__actions { margin-top: var(--kbq-size-l); }
    `,
})
export class ClarifyCardComponent {
    readonly request = input.required<QuestionRequest>();
    readonly submit = input.required<(payload: { answers: string[][] }) => Promise<boolean>>();

    protected readonly colors = KbqComponentColors;
    protected readonly customValue = CUSTOM;
    protected readonly selections = signal<QuestionSelection[]>([]);
    protected readonly sending = signal(false);
    protected readonly payload = computed(() => buildReplyPayload(this.request(), this.selections()));

    constructor() {
        // Only a new question request resets the choices: re-fetches of the same request keep half-typed answers.
        const requestId = computed(() => this.request().id);
        effect(() => {
            requestId();
            untracked(() => this.selections.set(this.request().questions.map(() => emptySelection())));
        });
    }

    protected custom = allowsCustom;

    protected radioValue(i: number): string | null {
        const s = this.selections()[i];
        if (!s) return null;
        return s.customOn ? CUSTOM : (s.selected[0] ?? null);
    }

    protected isChecked(i: number, label: string): boolean {
        return !!this.selections()[i]?.selected.includes(label);
    }

    protected pickRadio(i: number, value: string): void {
        this.patch(i, (s) =>
            value === CUSTOM ? { ...s, customOn: true } : { ...s, customOn: false, selected: [value] },
        );
    }

    protected toggle(i: number, label: string, checked: boolean): void {
        this.patch(i, (s) => ({ ...s, selected: checked ? [...s.selected, label] : s.selected.filter((l) => l !== label) }));
    }

    protected setCustom(i: number, on: boolean): void {
        this.patch(i, (s) => ({ ...s, customOn: on }));
    }

    protected setCustomText(i: number, text: string): void {
        this.patch(i, (s) => ({ ...s, customText: text }));
    }

    protected onKeydown(event: KeyboardEvent): void {
        if (!isSendShortcut(event)) return;
        event.preventDefault();
        void this.send();
    }

    protected async send(): Promise<void> {
        const payload = this.payload();
        if (!payload || this.sending()) return;
        this.sending.set(true);
        try {
            await this.submit()(payload);
        } finally {
            this.sending.set(false);
        }
    }

    private patch(i: number, fn: (s: QuestionSelection) => QuestionSelection): void {
        this.selections.update((all) => all.map((s, n) => (n === i ? fn(s) : s)));
    }
}
