import { ChangeDetectionStrategy, Component, DestroyRef, inject } from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { Router, RouterOutlet } from '@angular/router';
import { EventsService } from '../core/events/events.service';
import { ItemsStore } from '../core/items/items.store';
import { NotifyService } from '../core/notify/notify.service';
import { NavComponent } from './nav/nav.component';
import { isSearchShortcut } from './search-shortcut';

const SEARCH_OPEN = { paths: 'exact', queryParams: 'ignored', fragment: 'ignored', matrixParams: 'ignored' } as const;

/**
 * Signed-in layout: side menu + content area. Starts SSE and the chat list on enter, stops them on leave
 * (logout or 401), and turns final status changes into toasts.
 *
 * The routed screen's host is a flex column that fills the content area (see styles.scss).
 * The content area is its own stacking context: the sticky `kbq-top-bar` carries Koobiq's overlay z-index (1000),
 * the same as the cdk overlay container, so without it the screen header could stay above modal backdrops and
 * dropdowns. Isolated, it stays above the screen's content only.
 *
 * Cmd+K / Ctrl+K opens «Поиск и команды» (the `/search` route) from every signed-in screen, admin ones included,
 * even with focus in a field; when search is already open it does nothing, so there is never a second dialog.
 */
@Component({
    selector: 'v-shell',
    imports: [RouterOutlet, NavComponent],
    changeDetection: ChangeDetectionStrategy.OnPush,
    host: { class: 'v-shell', '(document:keydown)': 'onKeydown($event)' },
    template: `
        <v-nav />
        <main class="v-shell__main">
            <router-outlet />
        </main>
    `,
    styles: `
        :host { display: flex; height: 100%; min-height: 0; background: var(--kbq-background-bg); }
        .v-shell__main { position: relative; isolation: isolate; display: flex; flex-direction: column; flex: 1 1 auto; min-width: 0; min-height: 0; overflow: auto; }
    `,
})
export class ShellComponent {
    private readonly router = inject(Router);

    constructor() {
        const events = inject(EventsService);
        const store = inject(ItemsStore);
        const notify = inject(NotifyService);
        const destroyRef = inject(DestroyRef);

        events.start();
        void store.start();
        store.transitions$.pipe(takeUntilDestroyed(destroyRef)).subscribe((t) => notify.finalResult(t.item));
        destroyRef.onDestroy(() => {
            events.stop();
            store.setItems([]);
        });
    }

    protected onKeydown(event: KeyboardEvent): void {
        if (!isSearchShortcut(event)) return;
        // Also keeps the browser's own Cmd+K / Ctrl+K (address bar search) from firing.
        event.preventDefault();
        if (this.router.isActive('/search', SEARCH_OPEN)) return;
        void this.router.navigateByUrl('/search');
    }
}
