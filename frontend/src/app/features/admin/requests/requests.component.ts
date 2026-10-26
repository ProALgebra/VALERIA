import { ChangeDetectionStrategy, Component, computed, effect, inject, input, signal, untracked } from '@angular/core';
import { Router } from '@angular/router';
import { KbqBadgeColors, KbqBadgeModule } from '@koobiq/components/badge';
import { KbqButtonModule } from '@koobiq/components/button';
import { KbqComponentColors, KbqHighlightModule } from '@koobiq/components/core';
import { KbqEmptyStateModule } from '@koobiq/components/empty-state';
import {
    KbqFilter,
    KbqFilterBarModule,
    KbqPipe,
    KbqPipeTemplate,
    KbqPipeTypes,
    KbqSelectValue,
} from '@koobiq/components/filter-bar';
import { KbqIconModule } from '@koobiq/components/icon';
import { ApiService } from '../../../core/api/api.service';
import { matches, tokenize } from '../../../core/search/search';
import { ItemKind, KIND_ICON, adminStatusLabel, statusBadgeColor } from '../../../core/status/status';
import { PageStateComponent } from '../../../shared/page-state/page-state.component';
import { ScreenHeaderComponent } from '../../../shell/screen-header/screen-header.component';
import { AdminRequest, AdminTask } from '../shared/admin.models';
import { ADMIN_GRID } from '../shared/admin-grid';
import { ADMIN_SEARCH_PIPE_ID, adminSearchPipe, adminSearchQuery } from '../shared/admin-search';
import { ADMIN_LIST_STYLES } from '../shared/admin-styles';
import { formatWhen } from '../shared/format';
import {
    listFromQuery,
    listToQuery,
    PERIODS,
    periodFromPipe,
    periodFromQuery,
    periodRange,
    periodToPipe,
    periodToQuery,
} from '../shared/period';
import { KIND_LABEL } from '../shared/requests.helpers';
import { multiSelectValues, pipeTrackKey, withEpoch } from '../shared/pipe-track';
import { REQUEST_NO_FILTERS, requestFilterParams, RequestFilters, requestsByTasks } from './requests.filters';

const KINDS: ItemKind[] = ['question', 'task', 'folder'];

/** «Запросы пользователей» (`/admin/requests`, SPEC §10.5): every user's questions, task runs and folder work. */
@Component({
    selector: 'v-admin-requests',
    imports: [
        ScreenHeaderComponent,
        PageStateComponent,
        ADMIN_GRID,
        KbqFilterBarModule,
        KbqHighlightModule,
        KbqBadgeModule,
        KbqButtonModule,
        KbqIconModule,
        KbqEmptyStateModule,
    ],
    changeDetection: ChangeDetectionStrategy.OnPush,
    template: `
        <v-screen-header title="Запросы пользователей">
            <ng-container vHeaderAfterTitle>
                @if (!loading() && !error()) {
                    <kbq-badge [badgeColor]="badgeColors.FadeContrast">
                        {{ rows().length }}{{ filtered() && total() !== null ? ' из ' + total() : '' }}
                    </kbq-badge>
                }
            </ng-container>
        </v-screen-header>

        <div class="v-content">
            <div class="v-filters">
                <kbq-filter-bar [filter]="filter()" [pipeTemplates]="templates()" (onChangePipe)="onPipe($event)">
                    @for (pipe of filter().pipes; track pipeKey(pipe)) {
                        <ng-container *kbqPipe="pipe" />
                    }
                    @if (filtered()) {
                        <kbq-filter-reset (onResetFilter)="resetAll()" />
                    }
                </kbq-filter-bar>
            </div>

            <v-page-state [loading]="loading()" [error]="error()" (retry)="load()">
                @if (rows().length) {
                    <v-admin-grid class="v-mt8" [rows]="rows()" [rowId]="rowId" openable (rowOpen)="open($event)">
                        <ng-template vCol="title" header="Запрос" [flex]="1" [minWidth]="240" let-r>
                            <span class="v-title" [innerHTML]="r.title | kbqHighlightBackground: tokens() : true"></span>
                            <span class="v-sub">
                                <i kbq-icon class="v-subic" [class]="kindIcon(r.kind)" aria-hidden="true"></i>
                                <span [innerHTML]="kindSub(r) | kbqHighlightBackground: tokens() : true"></span>
                            </span>
                        </ng-template>
                        <ng-template vCol="user" header="Пользователь" kind="nowrap" let-r>
                            <span [innerHTML]="r.user.login || dash | kbqHighlightBackground: tokens() : true"></span>
                        </ng-template>
                        <ng-template vCol="status" header="Статус" kind="line" let-r>
                            <kbq-badge [badgeColor]="badgeColor(r)">{{ status(r) }}</kbq-badge>
                        </ng-template>
                        <ng-template vCol="created" header="Создан" kind="nowrap" let-r>
                            <span class="v-secondary">{{ when(r.created_at) }}</span>
                        </ng-template>
                    </v-admin-grid>
                } @else {
                    <kbq-empty-state class="v-empty">
                        <div kbq-empty-state-title>Запросов не нашли</div>
                        <div kbq-empty-state-text>Ищем по тексту запроса и логину с учётом фильтров.</div>
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
    `,
    styles: [
        ADMIN_LIST_STYLES,
        `
            .v-content {
                flex: 1 1 auto;
                min-height: 0;
                height: auto;
            }
            .v-mt8 {
                margin-top: 8px;
            }
            .v-title {
                display: block;
                color: var(--kbq-foreground-contrast);
            }
            .v-subic {
                margin-right: 4px;
                vertical-align: -3px;
                color: var(--kbq-icon-contrast-fade);
            }
        `,
    ],
})
export class RequestsComponent {
    readonly user = input<string>();
    readonly period = input<string>();
    readonly from = input<string>();
    readonly to = input<string>();
    readonly type = input<string>();
    readonly task = input<string>();
    readonly q = input<string>('');

    private readonly api = inject(ApiService);
    private readonly router = inject(Router);

    protected readonly colors = KbqComponentColors;
    protected readonly badgeColors = KbqBadgeColors;
    protected readonly dash = '—';
    /** Bumped after «Сбросить» and when task titles arrive, so the pipes are re-created (see pipeTrackKey). */
    private readonly pipeEpoch = signal(0);
    protected readonly pipeKey = pipeTrackKey;

    protected readonly loading = signal(true);
    protected readonly error = signal<unknown>(null);
    private readonly items = signal<AdminRequest[]>([]);
    /** Unfiltered list: total for «N из M» and the user pipe options. */
    private readonly all = signal<AdminRequest[] | null>(null);
    private readonly tasks = signal<AdminTask[]>([]);
    protected readonly total = computed(() => this.all()?.length ?? null);

    protected readonly state = computed<RequestFilters>(() => ({
        users: listFromQuery(this.user()),
        period: periodFromQuery(this.period(), this.from(), this.to()),
        types: listFromQuery(this.type()).filter((t): t is ItemKind => (KINDS as string[]).includes(t)),
        tasks: [...new Set(listFromQuery(this.task()))],
    }));
    protected readonly pipesFiltered = computed(() => {
        const s = this.state();
        return !!(s.users.length || s.period || s.types.length || s.tasks.length);
    });
    protected readonly filtered = computed(() => this.pipesFiltered() || !!this.q()?.trim());

    /** Search tokens: filter the rows and are highlighted in the cells. */
    protected readonly tokens = computed(() => tokenize(this.q() ?? ''));
    protected readonly rows = computed(() => {
        const tokens = this.tokens();
        return this.items().filter((r) => matches([r.title, r.user.login, KIND_LABEL[r.kind], r.task_title], tokens));
    });
    protected readonly rowId = (r: AdminRequest): string => r.kind + r.id;

    private readonly userOptions = computed<KbqSelectValue[]>(() => {
        const logins = new Set<string>(this.state().users);
        for (const r of this.all() ?? []) if (r.user.login) logins.add(r.user.login);
        return [...logins].sort().map((login) => ({ name: login, value: login, id: login }));
    });
    private readonly kindOptions: KbqSelectValue[] = KINDS.map((k) => ({ name: KIND_LABEL[k], value: k, id: k }));
    /** Tasks of the list; ids from the URL that are not (yet) known keep the raw id as the name. */
    private readonly taskOptions = computed<KbqSelectValue[]>(() => {
        const options = this.tasks().map((t) => ({ name: t.title, value: t.id, id: t.id }));
        const unknown = this.state().tasks.filter((id) => !options.some((o) => o.value === id));
        return [...options, ...unknown.map((id) => ({ name: id, value: id, id }))];
    });

    protected readonly templates = computed<KbqPipeTemplate[]>(() => [
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
            name: 'Период',
            id: 'period',
            type: KbqPipeTypes.Datetime,
            values: [...PERIODS],
            cleanable: true,
            removable: false,
            disabled: false,
        },
        {
            name: 'Тип',
            id: 'type',
            type: KbqPipeTypes.MultiSelect,
            values: this.kindOptions,
            cleanable: true,
            removable: false,
            disabled: false,
            compareWith: byValue,
        },
        {
            name: 'Задача',
            id: 'task',
            type: KbqPipeTypes.MultiSelect,
            values: this.taskOptions(),
            search: true,
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
        const types = this.kindOptions.filter((o) => s.types.includes(o.value as ItemKind));
        const pipes: KbqPipe[] = [
            // Read once when the pipe is created; typing must not rebuild the filter.
            adminSearchPipe('Текст запроса или логин', untracked(() => this.q())),
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
                name: 'Период',
                id: 'period',
                type: KbqPipeTypes.Datetime,
                value: periodToPipe(s.period),
                cleanable: true,
                removable: false,
                disabled: false,
            },
            {
                name: 'Тип',
                id: 'type',
                type: KbqPipeTypes.MultiSelect,
                value: types,
                cleanable: true,
                removable: false,
                disabled: false,
            },
        ];
        if (s.types.includes('task')) {
            const tasks = this.taskOptions().filter((o) => s.tasks.includes(o.value as string));
            pipes.push({
                name: 'Задача',
                id: 'task',
                type: KbqPipeTypes.MultiSelect,
                value: tasks,
                search: true,
                cleanable: true,
                removable: false,
                disabled: false,
            });
        }
        return { name: '', pipes: withEpoch(pipes, epoch), readonly: false, disabled: false, changed: false, saved: false };
    });

    private loadSeq = 0;

    constructor() {
        void this.loadStatic();
        effect(() => {
            const s = this.state();
            untracked(() => void this.load(s));
        });
    }

    private async loadStatic(): Promise<void> {
        const [all, tasks] = await Promise.allSettled([
            this.api.get<AdminRequest[]>('/api/admin/requests'),
            this.api.get<AdminTask[]>('/api/admin/tasks'),
        ]);
        if (all.status === 'fulfilled') this.all.set(all.value);
        if (tasks.status === 'fulfilled') {
            this.tasks.set(tasks.value);
            // A `?task=` deep link: the chip was created with raw ids, re-create it with the titles.
            if (this.state().tasks.length) this.pipeEpoch.update((n) => n + 1);
        }
    }

    async load(state: RequestFilters = this.state()): Promise<void> {
        const seq = ++this.loadSeq;
        this.loading.set(true);
        this.error.set(null);
        try {
            const params = requestFilterParams(state, periodRange(state.period));
            const items = await this.api.get<AdminRequest[]>('/api/admin/requests', { params });
            if (seq === this.loadSeq) this.items.set(requestsByTasks(items, state));
        } catch (err) {
            if (seq === this.loadSeq) this.error.set(err);
        } finally {
            if (seq === this.loadSeq) this.loading.set(false);
        }
    }

    protected onPipe(pipe: KbqPipe): void {
        const s = this.state();
        if (pipe.id === ADMIN_SEARCH_PIPE_ID) this.setQuery(adminSearchQuery(pipe.value));
        else if (pipe.id === 'user') void this.navigate({ ...s, users: multiSelectValues(pipe.value) });
        else if (pipe.id === 'period') void this.navigate({ ...s, period: periodFromPipe(pipe.value) });
        else if (pipe.id === 'type') {
            const types = multiSelectValues(pipe.value) as ItemKind[];
            void this.navigate({ ...s, types, tasks: types.includes('task') ? s.tasks : [] });
        } else if (pipe.id === 'task') void this.navigate({ ...s, tasks: multiSelectValues(pipe.value) });
    }

    private navigate(s: RequestFilters, extra: Record<string, string | null> = {}): Promise<boolean> {
        return this.router.navigate([], {
            queryParams: {
                user: listToQuery(s.users),
                ...periodToQuery(s.period),
                type: listToQuery(s.types),
                task: listToQuery(s.tasks),
                ...extra,
            },
            queryParamsHandling: 'merge',
        });
    }

    /** «Сбросить»: no filters, no search; the pipes are re-created once the URL is cleared. */
    protected resetAll(): void {
        void this.navigate(REQUEST_NO_FILTERS, { q: null }).then(() => this.pipeEpoch.update((n) => n + 1));
    }

    protected setQuery(q: string): void {
        void this.router.navigate([], {
            queryParams: { q: q || null },
            queryParamsHandling: 'merge',
            replaceUrl: true,
        });
    }

    protected open(r: AdminRequest): void {
        void this.router.navigate(['/admin/requests', r.id]);
    }

    protected kindIcon(kind: ItemKind): string {
        return KIND_ICON[kind] ?? KIND_ICON.question;
    }

    protected kindLabel(kind: ItemKind): string {
        return KIND_LABEL[kind] ?? kind;
    }

    protected kindSub(r: AdminRequest): string {
        return this.kindLabel(r.kind) + (r.task_title ? ' · ' + r.task_title : '');
    }

    protected status(r: AdminRequest): string {
        return adminStatusLabel(r.status, r.kind);
    }

    protected badgeColor(r: AdminRequest): KbqBadgeColors {
        return statusBadgeColor(r.status, r.kind);
    }

    protected when(iso: string): string {
        return formatWhen(iso);
    }
}

function byValue(a: KbqSelectValue | null | undefined, b: KbqSelectValue | null | undefined): boolean {
    return !!a && !!b && a.value === b.value;
}
