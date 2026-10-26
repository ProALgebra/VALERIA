import { ChangeDetectionStrategy, Component, computed, effect, ElementRef, inject, input, output, signal, untracked, viewChild } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { KbqFormFieldModule } from '@koobiq/components/form-field';
import { KbqTextareaModule } from '@koobiq/components/textarea';
import { UiStateService } from '../../core/state/ui-state.service';
import { composerState, isSendShortcut } from './send-action';
import { ComposerBoxComponent } from './composer-box.component';
import { SendButtonComponent } from './send-button.component';

/**
 * Message input in a `v-composer-box`: the textarea on top, the send button bottom-right inside the same frame.
 * The draft lives in sessionStorage per chat. `submit` performs the request and resolves `true` on success; the
 * button shows progress until then and the draft is cleared only after success. Ctrl/⌘ Enter sends. Projected
 * content is the composer bar (agent, model); it sits bottom-left in the frame, on the send button's row.
 * While the chat works, `stoppable` swaps the send button for «Остановить», which emits `stop`.
 */
@Component({
    selector: 'v-chat-composer',
    imports: [FormsModule, KbqFormFieldModule, KbqTextareaModule, ComposerBoxComponent, SendButtonComponent],
    changeDetection: ChangeDetectionStrategy.OnPush,
    template: `
        <v-composer-box [disabled]="state().locked">
            <kbq-form-field class="v-composer-box__field" noBorders>
                <textarea
                    #area
                    kbqTextarea
                    [canGrow]="false"
                    rows="2"
                    class="v-inputbox__area"
                    [placeholder]="placeholder()"
                    [attr.aria-label]="placeholder()"
                    [disabled]="state().locked"
                    [ngModel]="draft()"
                    (ngModelChange)="onDraft($event)"
                    (keydown)="onKeydown($event)"
                ></textarea>
            </kbq-form-field>
            @if (stoppable()) {
                <v-send-button
                    label="Остановить"
                    icon="kbq-stop-circle_16"
                    [shortcut]="false"
                    [sending]="stopping()"
                    (pressed)="stop.emit()"
                    >Остановить</v-send-button
                >
            } @else {
                <v-send-button
                    [label]="label()"
                    [disabled]="state().disabled"
                    [sending]="sending()"
                    [reason]="state().reason"
                    (pressed)="send()"
                    >{{ label() }}</v-send-button
                >
            }
            <ng-content />
        </v-composer-box>
    `,
    styles: `
        :host { display: block; }
        kbq-form-field { display: block; }
        .v-inputbox__area { display: block; width: 100%; resize: none; box-sizing: border-box; }
    `,
})
export class ChatComposerComponent {
    /** sessionStorage draft key, usually the chat id. */
    readonly draftKey = input.required<string>();
    readonly placeholder = input.required<string>();
    readonly label = input.required<string>();
    readonly busy = input(false);
    readonly busyReason = input('');
    readonly emptyReason = input('');
    readonly submit = input.required<(text: string) => Promise<boolean>>();
    /** Show «Остановить» in place of the send button. */
    readonly stoppable = input(false);
    /** The stop request is in flight. */
    readonly stopping = input(false);
    readonly stop = output<void>();

    private readonly ui = inject(UiStateService);
    private readonly area = viewChild<ElementRef<HTMLTextAreaElement>>('area');

    protected readonly draft = signal('');
    protected readonly sending = signal(false);
    protected readonly state = computed(() =>
        composerState({
            busy: this.busy(),
            busyReason: this.busyReason(),
            emptyReason: this.emptyReason(),
            draft: this.draft(),
            sending: this.sending(),
        }),
    );

    constructor() {
        effect(() => {
            const key = this.draftKey();
            untracked(() => this.draft.set(this.ui.readDraft(key)));
        });
    }

    focus(): void {
        this.area()?.nativeElement.focus();
    }

    protected onDraft(text: string): void {
        this.draft.set(text);
        this.ui.writeDraft(this.draftKey(), text);
    }

    protected onKeydown(event: KeyboardEvent): void {
        if (!isSendShortcut(event)) return;
        event.preventDefault();
        void this.send();
    }

    protected async send(): Promise<void> {
        if (this.state().disabled) return;
        const text = this.draft().trim();
        const key = this.draftKey();
        this.sending.set(true);
        try {
            const ok = await this.submit()(text);
            if (ok) {
                this.ui.clearDraft(key);
                if (this.draftKey() === key) this.draft.set('');
            }
        } finally {
            this.sending.set(false);
        }
    }
}
