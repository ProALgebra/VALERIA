import { ChangeDetectionStrategy, Component, computed, inject } from '@angular/core';
import { KbqResizable, KbqResizer } from '@koobiq/components/resizer';
import { CHAT_ASIDE_WIDTH, UI_KEYS, UiStateService } from '../../core/state/ui-state.service';
import { effectivePanelWidth, panelMaxWidth, ViewportService } from './panel-width';

/**
 * Resizable chat aside (folder work, task run). Shown while `valeria.chat.aside.open` is true; the screen header
 * toggle flips it. Width 240–560, default 320, saved to `valeria.chat.aside.width` on every `sizeChange`.
 *
 * ```html
 * <div class="screen-row"><main>…</main><v-chat-aside>…aside content…</v-chat-aside></div>
 * ```
 */
@Component({
    selector: 'v-chat-aside',
    imports: [KbqResizable, KbqResizer],
    changeDetection: ChangeDetectionStrategy.OnPush,
    host: {
        class: 'v-chat-aside',
        '[class.v-chat-aside_hidden]': '!open()',
        '[attr.aria-hidden]': '!open()',
    },
    template: `
        @if (open()) {
            <aside
                class="v-chat-aside__panel"
                kbqResizable
                [style.width.px]="width()"
                [style.min-width.px]="limits.min"
                [style.max-width.px]="maxWidth()"
            >
                <div class="v-chat-aside__resizer" [kbqResizer]="[-1, 0]" (sizeChange)="onResize($event.width)"></div>
                <div class="v-chat-aside__body"><ng-content /></div>
            </aside>
        }
    `,
    styles: `
        :host { display: flex; flex: none; min-height: 0; }
        :host(.v-chat-aside_hidden) { display: none; }
        .v-chat-aside__panel {
            position: relative;
            box-sizing: border-box;
            display: flex;
            flex-direction: column;
            border-left: 1px solid var(--kbq-line-contrast-less);
            background: var(--kbq-background-bg);
        }
        .v-chat-aside__body { flex: 1 1 auto; min-height: 0; overflow: auto; padding: var(--kbq-size-l); }
        .v-chat-aside__resizer {
            position: absolute;
            z-index: 2;
            top: 0;
            bottom: 0;
            left: 0;
            width: var(--kbq-size-xs);
            cursor: ew-resize;
        }
        .v-chat-aside__resizer:hover,
        .v-chat-aside__resizer:active { background: var(--kbq-line-theme-fade); }
    `,
})
export class ChatAsideComponent {
    private readonly ui = inject(UiStateService);
    private readonly viewport = inject(ViewportService);
    private readonly saved = this.ui.state(UI_KEYS.chatAsideWidth);

    protected readonly limits = CHAT_ASIDE_WIDTH;
    protected readonly open = this.ui.state(UI_KEYS.chatAsideOpen);
    protected readonly width = computed(() => effectivePanelWidth(this.saved(), CHAT_ASIDE_WIDTH.min, this.viewport.width()));
    /** A drag stops where the content would get narrower than 480 px. */
    protected readonly maxWidth = computed(() =>
        panelMaxWidth(CHAT_ASIDE_WIDTH.min, CHAT_ASIDE_WIDTH.max, this.viewport.width()),
    );

    protected onResize(width: number): void {
        if (!Number.isFinite(width)) return;
        this.saved.set(Math.round(Math.min(this.maxWidth(), Math.max(CHAT_ASIDE_WIDTH.min, width))));
    }
}
