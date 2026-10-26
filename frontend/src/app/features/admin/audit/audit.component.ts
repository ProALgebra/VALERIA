import { ChangeDetectionStrategy, Component, computed, effect, inject, input, signal, untracked } from '@angular/core';
import { Router } from '@angular/router';
import { KbqButtonModule } from '@koobiq/components/button';
import { kbqCodeBlockHighlightJsConfigProvider, KbqCodeBlockModule } from '@koobiq/components/code-block';
import { KbqComponentColors, KbqHighlightModule } from '@koobiq/components/core';
import { KbqContentPanelModule } from '@koobiq/components/content-panel';
import { KbqDlModule } from '@koobiq/components/dl';
import { KbqEmptyStateModule } from '@koobiq/components/empty-state';
import {
    KbqFilter,
    KbqFilterBarModule,
    KbqPipe,
    KbqPipeTemplate,
    KbqPipeTypes,
    KbqSelectValue,
} from '@koobiq/components/filter-bar';
import { KbqProgressSpinnerModule } from '@koobiq/components/progress-spinner';
import { ApiService } from '../../../core/api/api.service';
import { matches, tokenize } from '../../../core/search/search';
import { PageStateComponent } from '../../../shared/page-state/page-state.component';
import { cpanelWidth, CpanelWidthDirective } from '../../../shell/aside/panel-width';
import { ScreenHeaderComponent } from '../../../shell/screen-header/screen-header.component';
import { AdminRequest, AdminTask, AdminUser } from '../shared/admin.models';
import { ADMIN_GRID } from '../shared/admin-grid';
import { ADMIN_SEARCH_PIPE_ID, adminSearchPipe, adminSearchQuery } from '../shared/admin-search';
import { ADMIN_LIST_STYLES } from '../shared/admin-styles';
import { AUDIT_CATEGORIES, AuditCategory, auditLabel } from '../shared/audit-labels';
import { DASH, formatWhen } from '../shared/format';
import { PERIODS, periodFromPipe, periodRange, periodToPipe } from '../shared/period';
import { multiSelectValues, pipeTrackKey, withEpoch } from '../shared/pipe-track';
import {
    AUDIT_NO_FILTERS,
    auditApiParams,
    AuditEvent,
    auditFiltered,
    auditFiltersFromQuery,
    auditFiltersToQuery,
    AuditFilters,
    auditMetadata,
    AuditObjectContext,
    auditObjectLabel,
} from './audit.filters';

const PAGE_SIZE = 100;
/** While searching or looking for a deep-linked event, pages are fetched up to this many events. */
const AUTO_LOAD_LIMIT = 2000;

interface AuditRow extends AuditEvent {
    key: string;
    label: string;
    object: string;
}

/** «Аудит» (`/admin/audit`, SPEC §10.6): who did what, with details of an event in the side panel. */
@Component({
    selector: 'v-admin-audit',
    // Only the JSON grammar: the payload is always JSON, the full highlight.js bundle is 1 MB.
    providers: [
        kbqCodeBlockHighlightJsConfigProvider({
            core: () => import('highlight.js/lib/core'),
            languages: { json: () => import('highlight.js/lib/languages/json') },
        }),
    ],
    imports: [
        KbqCodeBlockModule,
        ScreenHeaderComponent,
        PageStateComponent,
        ADMIN_GRID,
        CpanelWidthDirective,
        KbqFilterBarModule,
        KbqHighlightModule,
        KbqButtonModule,
        KbqEmptyStateModule,
        KbqContentPanelModule,
        KbqDlModule,
        KbqProgressSpinnerModule,
    ],
    changeDetection: ChangeDetectionStrategy.OnPush,
    template: `
        <v-screen-header title="Аудит" />

        <div class="v-body">
            <kbq-content-panel-container
                [opened]="!!selected()"
                [width]="panel.width()"
                [minWidth]="panel.min"
                [maxWidth]="panel.max()"
                (openedChange)="onPanelOpened($event)"
            >
                <div class="v-content" (scroll)="onScroll($event)">
                    <div class="v-filters">
                        <kbq-filter-bar
                            [filter]="filter()"
                            [pipeTemplates]="templates()"
                            (onChangePipe)="onPipe($event)"
                        >
                            @for (pipe of filter().pipes; track pipeKey(pipe)) {
                                <ng-container *kbqPipe="pipe" />
                            }
                            @if (filtered()) {
                                <kbq-filter-reset (onResetFilter)="resetAll()" />
                            }
                        </kbq-filter-bar>
                    </div>

                    <v-page-state class="v-mt8" [loading]="loading()" [error]="error()" (retry)="reload()">
                        @if (rows().length) {
                            <v-admin-grid
                                [rows]="rows()"
                                [rowId]="rowId"
                                [current]="event()"
                                openable
                                (rowOpen)="openEvent($event)"
                            >
                                <ng-template vCol="time" header="Время" kind="nowrap" let-a>
                                    <span class="v-secondary">{{ when(a.created_at) }}</span>
                                </ng-template>
                                <ng-template vCol="actor" header="Кто" kind="nowrap" let-a>
                                    <span [innerHTML]="a.actor_login || dash | kbqHighlightBackground: tokens() : true"></span>
                                    @if (a.effective_login && a.effective_login !== a.actor_login) {
                                        <span
                                            class="v-sub"
                                            [innerHTML]="'за ' + a.effective_login | kbqHighlightBackground: tokens() : true"
                                        ></span>
                                    }
                                </ng-template>
                                <ng-template vCol="action" header="Действие" [flex]="1" [minWidth]="200" let-a>
                                    <span [innerHTML]="a.label | kbqHighlightBackground: tokens() : true"></span>
                                    @if (selected()) {
                                        <span class="v-sub" [innerHTML]="a.object | kbqHighlightBackground: tokens() : true"></span>
                                    } @else {
                                        <span
                                            class="v-sub v-mono"
                                            [innerHTML]="a.action | kbqHighlightBackground: tokens() : true"
                                        ></span>
                                    }
                                </ng-template>
                                <ng-template vCol="object" header="Объект" [flex]="1" [minWidth]="200" [hidden]="!!selected()" let-a>
                                    <span [innerHTML]="a.object | kbqHighlightBackground: tokens() : true"></span>
                                </ng-template>
                            </v-admin-grid>
                            @if (loadingMore()) {
                                <div class="v-more"><kbq-progress-spinner mode="indeterminate" size="compact" /></div>
                            }
                        } @else if (!loadingMore()) {
                            <kbq-empty-state class="v-empty">
                                <div kbq-empty-state-title>Ничего не нашли</div>
                                <div kbq-empty-state-text>Ищем по действию, логину и объекту с учётом фильтров.</div>
                                @if (filtered()) {
                                    <div kbq-empty-state-actions>
                                        <button kbq-button [color]="colors.ContrastFade" (click)="resetAll()">
                                            Сбросить фильтры
                                        </button>
                                    </div>
                                }
                            </kbq-empty-state>
                        }
                    </v-page-state>
                </div>

                <kbq-content-panel>
                    @if (selected(); as a) {
                        <kbq-content-panel-header [vCpanelWidth]="panel">
                            <div kbqContentPanelHeaderTitle class="v-panel-title">{{ a.label }}</div>
                        </kbq-content-panel-header>
                    }
                    @if (selected(); as a) {
                        <kbq-content-panel-body>
                            <kbq-dl class="v-dl" [vertical]="false">
                                <kbq-dt>Когда</kbq-dt>
                                <kbq-dd>{{ when(a.created_at) }}</kbq-dd>
                                <kbq-dt>Кто</kbq-dt>
                                <kbq-dd>{{ a.actor_login || dash }}</kbq-dd>
                                @if (a.effective_login && a.effective_login !== a.actor_login) {
                                    <kbq-dt>За кого</kbq-dt>
                                    <kbq-dd>{{ a.effective_login }}</kbq-dd>
                                }
                                <kbq-dt>Объект</kbq-dt>
                                <kbq-dd class="v-break-word">{{ a.object }}</kbq-dd>
                                <kbq-dt>Код действия</kbq-dt>
                                <kbq-dd
                                    ><span class="v-mono">{{ a.action }}</span></kbq-dd
                                >
                            </kbq-dl>
                            @if (metaJson(); as json) {
                                <section class="v-csec">
                                    <div class="v-csec__title">Подробности</div>
                                    <kbq-code-block [files]="[{ content: json, language: 'json' }]" [softWrap]="true" [canCopy]="false" />
                                </section>
                            }
                        </kbq-content-panel-body>
                    }
                </kbq-content-panel>
            </kbq-content-panel-container>
        </div>
    `,
    styles: [
        ADMIN_LIST_STYLES,
        `
            .v-mt8 {
                margin-top: var(--kbq-size-s);
            }
            .v-more {
                display: flex;
                justify-content: center;
                padding: 12px 0;
            }
            .v-break-word {
                overflow-wrap: anywhere;
            }
        `,
    ],
})
export class AuditComponent {
    readonly period = input<string>();
    readonly from = input<string>();
    readonly to = input<string>();
    readonly user = input<string>();
    readonly action = input<string>();
    readonly q = input<string>('');
    readonly event = input<string>();

    private readonly api = inject(ApiService);
    private readonly router = inject(Router);

    protected readonly colors = KbqComponentColors;
    protected readonly dash = DASH;
    protected readonly panel = cpanelWidth('audit');
    /** Bumped after «Сбросить» so the pipes are re-created with the cleared values (see pipeTrackKey). */
    private readonly pipeEpoch = signal(0);
    protected readonly pipeKey = pipeTrackKey;

    protected readonly loading = signal(true);
    protected readonly loadingMore = signal(false);
    protected readonly error = signal<unknown>(null);
    private readonly items = signal<AuditEvent[]>([]);
    private readonly nextCursor = signal<string | null>(null);
    private readonly ctx = signal<AuditObjectContext>({
        users: new Map(),
        tasks: new Map(),
        queues: new Map(),
        requests: new Map(),
    });
    /** Logins for the «Пользователь» pipe. */
    private readonly logins = signal<string[]>([]);

    protected readonly state = computed<AuditFilters>(() =>
        auditFiltersFromQuery({
            period: this.period(),
            from: this.from(),
            to: this.to(),
            user: this.user(),
            action: this.action(),
        }),
    );
    protected readonly pipesFiltered = computed(() => auditFiltered(this.state()));
    protected readonly filtered = computed(() => this.pipesFiltered() || !!this.q()?.trim());

    private readonly allRows = computed<AuditRow[]>(() => {
        const ctx = this.ctx();
        return this.items().map((e) => ({
            ...e,
            key: String(e.id),
            label: auditLabel(e.action),
            object: auditObjectLabel(e, ctx),
        }));
    });
    /** Search tokens: filter the rows and are highlighted in the cells. */
    protected readonly tokens = computed(() => tokenize(this.q() ?? ''));
    protected readonly rowId = (a: AuditRow): string => a.key;
    protected readonly rows = computed(() => {
        const tokens = this.tokens();
        return this.allRows().filter((a) =>
            matches([a.label, a.action, a.actor_login, a.effective_login, a.object], tokens),
        );
    });
    protected readonly selected = computed(() => {
        const id = this.event();
        return id ? (this.allRows().find((a) => a.key === id) ?? null) : null;
    });
    protected readonly metaJson = computed(() => auditMetadata(this.selected()?.metadata_json ?? null));

    private readonly userOptions = computed<KbqSelectValue[]>(() => {
        const set = new Set([...this.logins(), ...this.state().users]);
        return [...set].sort().map((l) => ({ name: l, value: l, id: l }));
    });
    private readonly actionOptions: KbqSelectValue[] = AUDIT_CATEGORIES.map((c) => ({ name: c.name, value: c.id, id: c.id }));

    protected readonly templates = computed<KbqPipeTemplate[]>(() => [
        {
            name: 'Период',
            id: 'period',
            type: KbqPipeTypes.Datetime,
            values: [...PERIODS],
            cleanable: true,
            removable: false,
            disabled: false,
        },
        {
            name: 'Пользователь',
            id: 'user',
            type: KbqPipeTypes.MultiSelect,
            values: this.userOptions(),
            search: true,
            cleanable: true,
            removable: false,
            disabled: false,
            compareWith: byValue,
        },
        {
            name: 'Действие',
            id: 'action',
            type: KbqPipeTypes.MultiSelect,
            values: this.actionOptions,
            cleanable: true,
            removable: false,
            disabled: false,
            compareWith: byValue,
        },
    ]);

    protected readonly filter = computed<KbqFilter>(() => {
        const epoch = this.pipeEpoch();
        const s = this.state();
        const users = this.userOptions().filter((o) => s.users.includes(o.value as string));
        const actions = this.actionOptions.filter((o) => s.actions.includes(o.value as AuditCategory));
        const pipes: KbqPipe[] = [
            // Read once when the pipe is created; typing must not rebuild the filter.
            adminSearchPipe('Действие, логин или объект', untracked(() => this.q())),
            {
                name: 'Период',
                id: 'period',
                type: KbqPipeTypes.Datetime,
                value: periodToPipe(s.period),
                cleanable: true,
                removable: false,
                disabled: false,
            },
            {
                name: 'Пользователь',
                id: 'user',
                type: KbqPipeTypes.MultiSelect,
                value: users,
                search: true,
                cleanable: true,
                removable: false,
                disabled: false,
            },
            {
                name: 'Действие',
                id: 'action',
                type: KbqPipeTypes.MultiSelect,
                value: actions,
                cleanable: true,
                removable: false,
                disabled: false,
            },
        ];
        return { name: '', pipes: withEpoch(pipes, epoch), readonly: false, disabled: false, changed: false, saved: false };
    });

    private loadSeq = 0;

    constructor() {
        void this.loadContext();
        effect(() => {
            const s = this.state();
            untracked(() => void this.load(s));
        });
        // Search and a deep-linked event look past the first page: fetch more until found or the list ends.
        effect(() => {
            const searching = !!this.q()?.trim();
            const id = this.event();
            const missing = !!id && !this.selected();
            const cursor = this.nextCursor();
            if (!cursor || this.loading() || this.loadingMore()) return;
            if ((searching || missing) && this.items().length < AUTO_LOAD_LIMIT) untracked(() => void this.loadMore());
        });
    }

    /** Names for the «Объект» column and logins for the user pipe. Failures only degrade to raw ids. */
    private async loadContext(): Promise<void> {
        const [users, tasks, queues, requests] = await Promise.allSettled([
            this.api.get<AdminUser[]>('/api/admin/users'),
            this.api.get<AdminTask[]>('/api/admin/tasks'),
            this.api.get<{ id: string; name: string }[]>('/api/admin/queues'),
            this.api.get<AdminRequest[]>('/api/admin/requests'),
        ]);
        const u = users.status === 'fulfilled' ? users.value : [];
        const requestTitles = new Map<string, string>();
        if (requests.status === 'fulfilled') {
            for (const r of requests.value) {
                requestTitles.set(String(r.id), r.title);
                requestTitles.set(String(r.chat_id), r.title);
            }
        }
        this.ctx.set({
            users: new Map(u.map((x) => [String(x.id), x.login])),
            tasks: new Map((tasks.status === 'fulfilled' ? tasks.value : []).map((t) => [String(t.id), t.title])),
            queues: new Map((queues.status === 'fulfilled' ? queues.value : []).map((x) => [String(x.id), x.name])),
            requests: requestTitles,
        });
        this.logins.set(u.map((x) => x.login));
    }

    private params(s: AuditFilters): Record<string, string> {
        return { ...auditApiParams(s, periodRange(s.period)), limit: String(PAGE_SIZE) };
    }

    protected reload(): void {
        void this.load(this.state());
    }

    private async load(s: AuditFilters): Promise<void> {
        const seq = ++this.loadSeq;
        this.loading.set(true);
        this.loadingMore.set(false);
        this.error.set(null);
        try {
            const page = await this.api.get<{ items: AuditEvent[]; next_cursor: string | null }>('/api/admin/audit', {
                params: this.params(s),
            });
            if (seq !== this.loadSeq) return;
            this.items.set(page.items);
            this.nextCursor.set(page.next_cursor);
        } catch (err) {
            if (seq === this.loadSeq) this.error.set(err);
        } finally {
            if (seq === this.loadSeq) this.loading.set(false);
        }
    }

    private async loadMore(): Promise<void> {
        const cursor = this.nextCursor();
        if (!cursor || this.loading() || this.loadingMore()) return;
        const seq = this.loadSeq;
        this.loadingMore.set(true);
        try {
            const page = await this.api.get<{ items: AuditEvent[]; next_cursor: string | null }>('/api/admin/audit', {
                params: { ...this.params(this.state()), cursor },
            });
            if (seq !== this.loadSeq) return;
            this.items.update((list) => [...list, ...page.items]);
            this.nextCursor.set(page.next_cursor);
        } catch {
            // Keep what is loaded; scrolling again retries.
        } finally {
            if (seq === this.loadSeq) this.loadingMore.set(false);
        }
    }

    protected onScroll(e: Event): void {
        const el = e.target as HTMLElement;
        if (el.scrollHeight - el.scrollTop - el.clientHeight < 200) void this.loadMore();
    }

    protected onPipe(pipe: KbqPipe): void {
        const s = this.state();
        if (pipe.id === ADMIN_SEARCH_PIPE_ID) this.setQuery(adminSearchQuery(pipe.value));
        else if (pipe.id === 'period') void this.navigate({ ...s, period: periodFromPipe(pipe.value) });
        else if (pipe.id === 'user') void this.navigate({ ...s, users: multiSelectValues(pipe.value) });
        else if (pipe.id === 'action')
            void this.navigate({ ...s, actions: multiSelectValues(pipe.value) as AuditCategory[] });
    }

    private navigate(s: AuditFilters, extra: Record<string, string | null> = {}): Promise<boolean> {
        return this.router.navigate([], {
            queryParams: { ...auditFiltersToQuery(s), ...extra },
            queryParamsHandling: 'merge',
        });
    }

    /** «Сбросить»: no filters, no search; the pipes are re-created once the URL is cleared. */
    protected resetAll(): void {
        void this.navigate(AUDIT_NO_FILTERS, { q: null }).then(() => this.pipeEpoch.update((n) => n + 1));
    }

    protected setQuery(q: string): void {
        void this.router.navigate([], {
            queryParams: { q: q || null },
            queryParamsHandling: 'merge',
            replaceUrl: true,
        });
    }

    protected openEvent(a: AuditRow): void {
        void this.router.navigate([], { queryParams: { event: a.key }, queryParamsHandling: 'merge' });
    }

    protected onPanelOpened(opened: boolean): void {
        if (!opened && this.event())
            void this.router.navigate([], { queryParams: { event: null }, queryParamsHandling: 'merge' });
    }

    protected when(iso: string): string {
        return formatWhen(iso, { timeForOld: true });
    }
}

function byValue(a: KbqSelectValue | null | undefined, b: KbqSelectValue | null | undefined): boolean {
    return !!a && !!b && a.value === b.value;
}
