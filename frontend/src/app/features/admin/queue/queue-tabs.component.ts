import { ChangeDetectionStrategy, Component, input } from '@angular/core';
import { RouterLink } from '@angular/router';
import { KbqTabsModule } from '@koobiq/components/tabs';

/** «Сейчас» / «Очереди и лимиты»: two pages of «Очередь», so a nav bar with links rather than in-page tabs. */
@Component({
    selector: 'v-queue-tabs',
    imports: [KbqTabsModule, RouterLink],
    changeDetection: ChangeDetectionStrategy.OnPush,
    template: `
        <nav kbqTabNavBar aria-label="Очередь">
            <a kbqTabLink routerLink="/admin/queue" [active]="active() === 'live'">Сейчас</a>
            <a kbqTabLink routerLink="/admin/queue/settings" [active]="active() === 'settings'">Очереди и лимиты</a>
        </nav>
    `,
    styles: `:host { display: block; margin-bottom: var(--kbq-size-l); }`,
})
export class QueueTabsComponent {
    readonly active = input.required<'live' | 'settings'>();
}
