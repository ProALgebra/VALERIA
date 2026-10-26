import { ChangeDetectionStrategy, Component, input } from '@angular/core';
import { RouterLink } from '@angular/router';
import { KbqEmptyStateModule } from '@koobiq/components/empty-state';
import { KbqLinkModule } from '@koobiq/components/link';

/**
 * «Чат не найден» with a link home (SPEC §5): the chat was deleted, belongs to someone else or never existed.
 * Chat screens render it in their content area when the API answers 404.
 */
@Component({
    selector: 'v-not-found',
    imports: [RouterLink, KbqEmptyStateModule, KbqLinkModule],
    changeDetection: ChangeDetectionStrategy.OnPush,
    template: `
        <kbq-empty-state [alignTop]="true">
            <div kbq-empty-state-title>{{ title() }}</div>
            <div kbq-empty-state-actions>
                <a kbq-link routerLink="/">На главную</a>
            </div>
        </kbq-empty-state>
    `,
    styles: `
        :host { display: flex; flex-direction: column; flex: 1 1 auto; min-height: 0; padding-top: var(--kbq-size-7xl); }
    `,
})
export class NotFoundComponent {
    readonly title = input('Чат не найден');
}
