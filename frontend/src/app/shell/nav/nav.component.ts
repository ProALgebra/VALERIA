import { NgTemplateOutlet } from '@angular/common';
import {
    afterNextRender,
    afterRenderEffect,
    ChangeDetectionStrategy,
    Component,
    computed,
    DestroyRef,
    effect,
    ElementRef,
    inject,
    Injector,
    signal,
    viewChild,
    ViewEncapsulation,
} from '@angular/core';
import { takeUntilDestroyed, toSignal } from '@angular/core/rxjs-interop';
import { NavigationEnd, Router, RouterLink, RouterLinkActive } from '@angular/router';
import { KbqBadgeColors, KbqBadgeModule } from '@koobiq/components/badge';
import { KbqButtonModule } from '@koobiq/components/button';
import { KbqComponentColors, PopUpPlacements } from '@koobiq/components/core';
import { KbqDividerModule } from '@koobiq/components/divider';
import { KbqDropdownModule } from '@koobiq/components/dropdown';
import { KbqIconModule } from '@koobiq/components/icon';
import { KbqNavbarModule } from '@koobiq/components/navbar';
import { KbqProgressSpinnerModule } from '@koobiq/components/progress-spinner';
import { KbqTitleDirective } from '@koobiq/components/title';
import { KbqToolTipModule } from '@koobiq/components/tooltip';
import { filter, map } from 'rxjs';
import { AuthService } from '../../core/auth/auth.service';
import { Item, itemKey, itemRoute } from '../../core/items/item.model';
import { ItemsStore } from '../../core/items/items.store';
import { UI_KEYS, UiStateService } from '../../core/state/ui-state.service';
import { KIND_ICON, statusIcon, statusLabel } from '../../core/status/status';
import { AppThemeService, THEME_OPTIONS } from '../../core/theme/theme.service';
import { snoozeNavLabel } from '../../core/time/snooze';
import { ChatActionsService } from '../chat-menu/chat-actions.service';
import { ChatMenuComponent } from '../chat-menu/chat-menu.component';
import { searchShortcutAria, searchShortcutHint } from '../search-shortcut';

/** Admin sections of the «Система» submenu (SPEC §6.1 item 9, prototype `SYS`). */
export const SYSTEM_ENTRIES: readonly { link: string; label: string; icon: string }[] = [
    { link: '/admin/statistics', label: 'Статистика', icon: 'kbq-database_16' },
    { link: '/admin/users', label: 'Пользователи', icon: 'kbq-user-multiple_16' },
    { link: '/admin/tasks', label: 'Настройка задач', icon: 'kbq-gear_16' },
    { link: '/admin/queue', label: 'Очередь', icon: 'kbq-pulse_16' },
    { link: '/admin/requests', label: 'Запросы пользователей', icon: 'kbq-message_16' },
    { link: '/admin/audit', label: 'Аудит', icon: 'kbq-shield-check_16' },
];

/** Two letters for the avatar. The backend has no display name, so they come from the login. */
export function initialsOf(login: string): string {
    const parts = login.split(/[\s._-]+/).filter(Boolean);
    const letters = parts.length > 1 ? parts[0][0] + parts[1][0] : login.slice(0, 2);
    return letters.toLocaleUpperCase('ru');
}

/**
 * Vertical side menu (SPEC §6.1). Every `kbq-navbar-item` lives in this one template: the Koobiq navbar
 * finds its items with a content query, so rows rendered inside child components would fall out of its
 * roving focus.
 */
@Component({
    selector: 'v-nav',
    imports: [
        NgTemplateOutlet,
        RouterLink,
        RouterLinkActive,
        KbqBadgeModule,
        KbqNavbarModule,
        KbqButtonModule,
        KbqIconModule,
        KbqDropdownModule,
        KbqDividerModule,
        KbqTitleDirective,
        KbqToolTipModule,
        KbqProgressSpinnerModule,
        ChatMenuComponent,
    ],
    templateUrl: './nav.component.html',
    styleUrl: './nav.component.scss',
    encapsulation: ViewEncapsulation.None,
    changeDetection: ChangeDetectionStrategy.OnPush,
    host: { class: 'v-nav-host' },
})
export class NavComponent {
    protected readonly auth = inject(AuthService);
    protected readonly theme = inject(AppThemeService);
    private readonly store = inject(ItemsStore);
    private readonly router = inject(Router);
    private readonly host = inject<ElementRef<HTMLElement>>(ElementRef);
    private readonly injector = inject(Injector);
    private readonly ui = inject(UiStateService);

    protected readonly colors = KbqComponentColors;
    protected readonly badgeColors = KbqBadgeColors;
    protected readonly searchShortcutHint = searchShortcutHint();
    protected readonly searchShortcutAria = searchShortcutAria();
    protected readonly placements = PopUpPlacements;
    protected readonly systemEntries = SYSTEM_ENTRIES;
    protected readonly themeOptions = THEME_OPTIONS;

    protected readonly groups = this.store.groups;
    protected readonly snoozedOpen = this.ui.state(UI_KEYS.navSnoozedOpen);
    protected readonly archiveOpen = this.ui.state(UI_KEYS.navArchiveOpen);
    protected readonly toggle = (open: boolean) => !open;

    /** The chat list scrolls between the pinned blocks; a line above the bottom block shows while it overflows. */
    private readonly list = viewChild.required<string, ElementRef<HTMLElement>>('list', { read: ElementRef });
    protected readonly listOverflows = signal(false);

    private readonly url = toSignal(
        this.router.events.pipe(
            filter((e): e is NavigationEnd => e instanceof NavigationEnd),
            map((e) => e.urlAfterRedirects),
        ),
        { initialValue: this.router.url },
    );
    protected readonly inAdmin = computed(() => this.path().startsWith('/admin'));
    protected readonly initials = computed(() => initialsOf(this.auth.user()?.login ?? ''));
    private readonly path = computed(() => this.url().split(/[?#]/)[0]);

    /** Keys of the rows in menu order; the previous list is kept to find a deleted row's neighbour. */
    private visibleKeys: string[] = [];
    private previousKeys: string[] = [];
    private readonly orderedKeys = computed(() => {
        const g = this.groups();
        const rows = [...g.active, ...g.done];
        if (this.snoozedOpen()) rows.push(...g.snoozed);
        if (this.archiveOpen()) rows.push(...g.archive);
        return rows.map(itemKey);
    });

    constructor() {
        const destroyRef = inject(DestroyRef);
        effect(() => {
            this.previousKeys = this.visibleKeys;
            this.visibleKeys = this.orderedKeys();
        });
        // Rows and folds change the list height; window and layout changes resize the list itself.
        afterRenderEffect(() => {
            this.orderedKeys();
            this.measureList();
        });
        afterNextRender(() => {
            if (typeof ResizeObserver === 'undefined') return;
            const observer = new ResizeObserver(() => this.measureList());
            observer.observe(this.list().nativeElement);
            destroyRef.onDestroy(() => observer.disconnect());
        });
        inject(ChatActionsService)
            .removed$.pipe(takeUntilDestroyed(destroyRef))
            .subscribe((item) => this.focusNeighbourOf(itemKey(item)));
    }

    protected key = itemKey;
    protected route = itemRoute;
    protected kindIcon = (item: Item) => KIND_ICON[item.kind] ?? KIND_ICON.question;
    protected statusText = (item: Item) => statusLabel(item.status, item.kind);
    protected statusMark = (item: Item) => statusIcon(item.status, item.kind);
    protected returnTime = (item: Item) => (item.snoozed_until ? snoozeNavLabel(new Date(item.snoozed_until)) : '');
    protected isCurrent = (link: string) => this.path() === link || this.path().startsWith(`${link}/`);

    protected logout(): void {
        void this.auth.logout();
    }

    private measureList(): void {
        const el = this.list().nativeElement;
        this.listOverflows.set(el.scrollHeight > el.clientHeight + 1);
    }

    /** After a delete the focus goes to the next row (or the previous one), else to «Новый запрос». */
    private focusNeighbourOf(key: string): void {
        const list = this.visibleKeys.includes(key) ? this.visibleKeys : this.previousKeys;
        const index = list.indexOf(key);
        const candidates = index < 0 ? [] : [...list.slice(index + 1), ...list.slice(0, index).reverse()];
        afterNextRender(
            () => {
                const root = this.host.nativeElement;
                const target =
                    candidates
                        .map((k) => root.querySelector<HTMLElement>(`[data-key="${CSS.escape(k)}"]`))
                        .find((el) => !!el) ?? root.querySelector<HTMLElement>('[data-nav-home]');
                target?.focus();
            },
            { injector: this.injector },
        );
    }
}
