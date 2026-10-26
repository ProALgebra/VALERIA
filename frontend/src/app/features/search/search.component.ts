import {
    afterNextRender,
    ChangeDetectionStrategy,
    Component,
    computed,
    effect,
    DestroyRef,
    ElementRef,
    inject,
    Injector,
    input,
    signal,
    TemplateRef,
    untracked,
    viewChild,
} from '@angular/core';
import { Location } from '@angular/common';
import { Router } from '@angular/router';
import { KbqBadgeColors, KbqBadgeModule } from '@koobiq/components/badge';
import { KbqButtonModule } from '@koobiq/components/button';
import { KbqComponentColors, PopUpPlacements } from '@koobiq/components/core';
import { KbqEmptyStateModule } from '@koobiq/components/empty-state';
import { KbqFormFieldModule } from '@koobiq/components/form-field';
import { KbqIconModule } from '@koobiq/components/icon';
import { KbqInputModule } from '@koobiq/components/input';
import { KbqLinkModule } from '@koobiq/components/link';
import { KbqListModule } from '@koobiq/components/list';
import { KbqModalRef, KbqModalService, ModalSize } from '@koobiq/components/modal';
import { KbqToolTipModule } from '@koobiq/components/tooltip';
import { AuthService } from '../../core/auth/auth.service';
import { Item, itemKey, itemRoute } from '../../core/items/item.model';
import { ItemsStore } from '../../core/items/items.store';
import { KIND_ICON, statusBadgeColor, statusLabel } from '../../core/status/status';
import { TaskDef } from '../../shared/tasks/task.model';
import { TasksCatalog } from '../../shared/tasks/tasks-catalog.service';
import { HomeDrafts } from '../home/home-drafts';
import { HomeComponent } from '../home/home.component';
import { buildPalette, KIND_LABEL, PaletteCommand, rowTime } from './palette';

type Row =
    | { type: 'cmd'; key: string; cmd: PaletteCommand }
    | { type: 'task'; key: string; task: TaskDef }
    | { type: 'chat'; key: string; item: Item };

/**
 * «Поиск и команды» (`/search?q=`), SPEC §8.5 and §12. Shown over the home screen like the prototype:
 * the route replaces the outlet, so the screen renders home underneath (inert) and the palette on top.
 * The palette is a stock kbq-modal opened for the lifetime of the route: closing it leaves the route,
 * leaving the route closes it.
 * Searches only what is loaded: menu items (archive and snoozed included) and tasks.
 */
@Component({
    selector: 'v-search',
    imports: [
        HomeComponent,
        KbqBadgeModule,
        KbqButtonModule,
        KbqEmptyStateModule,
        KbqFormFieldModule,
        KbqIconModule,
        KbqInputModule,
        KbqLinkModule,
        KbqListModule,
        KbqToolTipModule,
    ],
    changeDetection: ChangeDetectionStrategy.OnPush,
    host: { class: 'v-search' },
    templateUrl: './search.component.html',
    styleUrl: './search.component.scss',
})
export class SearchComponent {
    /** `?q=` */
    readonly q = input<string | undefined>(undefined);

    private readonly router = inject(Router);
    private readonly location = inject(Location);
    private readonly auth = inject(AuthService);
    private readonly items = inject(ItemsStore);
    private readonly catalog = inject(TasksCatalog);
    private readonly drafts = inject(HomeDrafts);
    private readonly modal = inject(KbqModalService);

    protected readonly colors = KbqComponentColors;
    protected readonly badgeColors = KbqBadgeColors;
    protected readonly placements = PopUpPlacements;
    protected readonly kindIcon = KIND_ICON;
    protected readonly kindLabel = KIND_LABEL;
    protected readonly statusLabel = statusLabel;
    protected readonly statusBadgeColor = statusBadgeColor;

    private readonly input = viewChild<ElementRef<HTMLInputElement>>('input');
    private readonly body = viewChild.required<TemplateRef<object>>('body');
    private modalRef?: KbqModalRef;
    /** Set once the palette starts leaving, so Esc, the backdrop and the close button navigate only once. */
    private closing = false;
    /** Came here from another screen of the app: close goes back to it. */
    private readonly hasBack = !!this.router.lastSuccessfulNavigation?.previousNavigation;

    protected readonly query = signal('');
    private readonly now = signal(Date.now());
    protected readonly result = computed(() =>
        buildPalette({
            query: this.query(),
            items: this.items.items(),
            tasks: this.catalog.tasks(),
            tasksOnly: this.auth.tasksOnly(),
            workspace: this.auth.user()?.workspace_path,
            now: this.now(),
        }),
    );
    protected readonly rows = computed<Row[]>(() => {
        const r = this.result();
        if (r.nothing) return [];
        return [
            ...r.commands.map((cmd) => ({ type: 'cmd' as const, key: `cmd:${cmd.id}`, cmd })),
            ...r.tasks.map((task) => ({ type: 'task' as const, key: `task:${task.id}`, task })),
            ...r.chats.map((item) => ({ type: 'chat' as const, key: itemKey(item), item })),
        ];
    });
    protected readonly active = signal(0);
    protected readonly activeKey = computed(() => this.rows()[this.active()]?.key ?? null);

    constructor() {
        void this.catalog.ensure().catch(() => undefined);
        effect(() => {
            const q = this.q() ?? '';
            untracked(() => {
                if (q !== this.query()) this.query.set(q);
            });
        });
        afterNextRender(() => this.openModal());
        inject(DestroyRef).onDestroy(() => {
            this.closing = true;
            this.modalRef?.close();
        });
    }

    private openModal(): void {
        // No title and no footer: Koobiq renders no header, like its confirmation modal, and the search field
        // with its close button sits at the top of the content.
        this.modalRef = this.modal.create({
            kbqContent: this.body(),
            kbqSize: ModalSize.Medium,
            kbqClosable: false,
            kbqMaskClosable: true,
            kbqRestoreFocus: false,
            kbqOnCancel: () => this.close(),
        });
        afterNextRender(
            () => {
                // kbq-modal has no dialog role of its own; the e2e and assistive tech find the palette by it.
                // The container class is bound in the modal's first render, so this waits for it.
                const container = this.modalRef?.getElement().querySelector('.kbq-modal-container');
                container?.setAttribute('role', 'dialog');
                container?.setAttribute('aria-modal', 'true');
                container?.setAttribute('aria-label', 'Поиск и команды');
                // The modal focuses its first button on open; the palette starts in the search field.
                this.input()?.nativeElement.focus();
            },
            { injector: this.injectorRef },
        );
    }

    protected rowId(key: string): string {
        return `v-palette-row-${key.replace(/[^a-zA-Z0-9_-]/g, '_')}`;
    }

    protected indexOf(key: string): number {
        return this.rows().findIndex((r) => r.key === key);
    }

    protected time(item: Item): string {
        return rowTime(item.updated_at ?? item.created_at, this.now());
    }

    protected onInput(value: string): void {
        this.query.set(value);
        this.active.set(0);
        this.now.set(Date.now());
        void this.router.navigate([], {
            queryParams: { q: value || null },
            replaceUrl: true,
        });
    }

    protected onKeydown(event: KeyboardEvent): void {
        const count = this.rows().length;
        switch (event.key) {
            case 'ArrowDown':
                event.preventDefault();
                if (count) this.active.set((this.active() + 1) % count);
                this.scrollActive();
                break;
            case 'ArrowUp':
                event.preventDefault();
                if (count) this.active.set((this.active() - 1 + count) % count);
                this.scrollActive();
                break;
            case 'Enter': {
                const row = this.rows()[this.active()];
                if (row) {
                    event.preventDefault();
                    this.activate(row);
                }
                break;
            }
            case 'Escape':
                event.preventDefault();
                this.close();
                break;
        }
    }

    protected activate(row: Row): void {
        switch (row.type) {
            case 'cmd':
                if (row.cmd.id === 'ask') this.askValeria();
                else {
                    this.drafts.setMode('do');
                    void this.router.navigate(['/']);
                }
                break;
            case 'task':
                void this.router.navigate(['/new', row.task.id]);
                break;
            case 'chat':
                void this.router.navigateByUrl(itemRoute(row.item));
                break;
        }
    }

    /** «Спросить» and «спросить об этом Valeria»: home in question mode with the query in the field. */
    protected askValeria(): void {
        const text = this.query().trim();
        this.drafts.setMode('ask');
        if (text) this.drafts.setText(text);
        void this.router.navigate(['/']);
    }

    protected close(): void {
        if (this.closing) return;
        this.closing = true;
        if (this.hasBack) this.location.back();
        else void this.router.navigate(['/']);
    }

    private readonly injectorRef = inject(Injector);

    private scrollActive(): void {
        const key = this.activeKey();
        if (!key) return;
        afterNextRender(
            () => document.getElementById(this.rowId(key))?.scrollIntoView({ block: 'nearest' }),
            { injector: this.injectorRef },
        );
    }
}
