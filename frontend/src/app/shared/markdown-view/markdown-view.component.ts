import { ChangeDetectionStrategy, Component, input } from '@angular/core';
import { KbqMarkdownModule, kbqMarkdownMarkedOptionsProvider } from '@koobiq/components/markdown';
import { SAFE_MARKED_OPTIONS } from './safe-markdown';

/**
 * Agent answer rendered through `kbq-markdown` with the sanitizing renderer (raw HTML escaped, unsafe links dropped).
 * Stock Koobiq markdown look. Used for chat answers and the task-run report alike.
 */
@Component({
    selector: 'v-markdown-view',
    imports: [KbqMarkdownModule],
    providers: [kbqMarkdownMarkedOptionsProvider(SAFE_MARKED_OPTIONS)],
    changeDetection: ChangeDetectionStrategy.OnPush,
    template: `
        @if (text()) {
            <kbq-markdown class="v-md" [markdownText]="text() ?? null" />
        }
    `,
    styles: `
        :host { display: block; min-width: 0; }
    `,
})
export class MarkdownViewComponent {
    readonly text = input<string | null | undefined>('');
}
