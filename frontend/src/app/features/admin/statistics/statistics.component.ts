import { ChangeDetectionStrategy, Component, computed, effect, inject, signal, TemplateRef, viewChild } from '@angular/core';
import { toSignal } from '@angular/core/rxjs-interop';
import { ActivatedRoute, Router } from '@angular/router';
import { DateFormatter } from '@koobiq/components/core';
import { KbqEmptyStateModule } from '@koobiq/components/empty-state';
import { KbqFilter, KbqFilterBarModule, KbqPipe, KbqPipeTemplate, KbqPipeTypes } from '@koobiq/components/filter-bar';
import { PageStateComponent } from '../../../shared/page-state/page-state.component';
import { ScreenHeaderComponent } from '../../../shell/screen-header/screen-header.component';
import { AdminConfigApi, AdminTask, StatRunsResponse } from '../config-shared/admin-config.api';
import { dur, EMPTY, plural } from '../config-shared/format';
import { sortByName } from '../config-shared/names';
import { ControlChartComponent } from './control-chart.component';
import { PeriodChipFormatter } from './period-formatter';
import { assignTaskShapes, ShapeComponent, ShapeName } from './shape.component';
import { multiSelectValues, pipeTrackKey, withEpoch } from '../shared/pipe-track';
import {
    DEFAULT_PERIOD,
    kpis,
    parsePeriod,
    parseTypes,
    Period,
    periodParam,
    periodRange,
    PERIODS,
    runsByTypes,
    toChartRun,
    typeParam,
} from './stats-math';

const PERIOD_PIPE = 'Период';
const TYPE_PIPE = 'Тип';

interface TypeOption {
    name: string;
    value: string;
    id: string;
    /** Mark shape shown before the name in the dropdown, as on the chart; none for an unknown id. */
    shape: ShapeName | null;
}

/**
 * «Статистика» (SPEC §10.1, prototype step «stats»): filter bar (period is required, types are optional and
 * multiple), four KPIs, the control chart with a histogram, and the data source line. Filters live in
 * `?period=&type=a,b`; no `type` means every type.
 */
@Component({
    selector: 'v-statistics',
    imports: [
        ScreenHeaderComponent,
        PageStateComponent,
        KbqFilterBarModule,
        KbqEmptyStateModule,
        ControlChartComponent,
        ShapeComponent,
    ],
    providers: [{ provide: DateFormatter, useClass: PeriodChipFormatter }],
    changeDetection: ChangeDetectionStrategy.OnPush,
    template: `
        <v-screen-header title="Статистика" />
        <v-page-state [loading]="loading()" [error]="error()" (retry)="load()">
            <div class="v-content">
                <kbq-filter-bar [filter]="filter()" [pipeTemplates]="templates()" (onChangePipe)="onPipe($event)">
                    @for (pipe of filter().pipes; track pipeKey(pipe)) {
                        <ng-container *kbqPipe="pipe" />
                    }
                    @if (filtered()) {
                        <kbq-filter-reset (onResetFilter)="resetAll()" />
                    }
                </kbq-filter-bar>

                @let k = stats();
                <div class="v-kpis">
                    <div class="v-kpi">
                        <div class="v-kpi__v">{{ k.count }}</div>
                        <div class="v-kpi__t">{{ countText(k.count) }} за период</div>
                    </div>
                    <div class="v-kpi">
                        <div class="v-kpi__v">{{ k.medianWork === null ? EMPTY : dur(k.medianWork) }}</div>
                        <div class="v-kpi__t">медиана работы агента</div>
                    </div>
                    <div class="v-kpi">
                        <div class="v-kpi__v">{{ k.medianWait === null ? EMPTY : dur(k.medianWait) }}</div>
                        <div class="v-kpi__t">медиана ожидания в очереди</div>
                    </div>
                    <div class="v-kpi">
                        <div class="v-kpi__v">{{ k.failed }}</div>
                        <div class="v-kpi__t">с ошибкой{{ k.failedPct === null ? '' : ' · ' + k.failedPct + '%' }}</div>
                    </div>
                </div>

                <section class="v-chart">
                    @if (runs().length) {
                        <v-control-chart [runs]="runs()" [from]="range().from" [to]="range().to" [now]="now()" [shapes]="shapes()" />
                        <dl class="v-legend" aria-label="Легенда">
                            <dt>Метка</dt>
                            <dd>один запуск: по горизонтали — когда встал в очередь, по вертикали — сколько работал агент</dd>
                            <dt>Форма</dt>
                            <dd>
                                @for (key of keys(); track key.shapeKey) {
                                    <span class="v-key"><svg [vShape]="key.shape" st="key"></svg>{{ key.label }}</span>
                                }
                            </dd>
                            <dt>Цвет</dt>
                            <dd>
                                <span class="v-key"><svg vShape="circle" st="done"></svg>успешно</span>
                                <span class="v-key"><svg vShape="circle" st="failed"></svg>с ошибкой</span>
                                <span class="v-key"><svg vShape="circle" st="cancelled"></svg>отменён</span>
                                <span class="v-key"><svg vShape="circle" st="running"></svg>ещё идёт</span>
                            </dd>
                            <dt>Черта над меткой</dt>
                            <dd><span class="v-key"><i class="v-key__stem"></i>ожидание в очереди</span></dd>
                        </dl>
                    } @else if (data()) {
                        <kbq-empty-state class="v-chart__empty">
                            <div kbq-empty-state-title>За этот период запусков нет</div>
                            <div kbq-empty-state-text>Выберите другой период или тип.</div>
                        </kbq-empty-state>
                    }
                </section>

                <p class="v-source">
                    Источник: вопросы и запуски задач VALERIA, часовой пояс Europe/Moscow, данные с 27 июля 2026. Медиана и 90% считаются по
                    успешно завершённым запускам. Работа в папке не входит: это диалог, его длительность зависит от человека.
                </p>
            </div>
        </v-page-state>
        <ng-template #typeOption let-option="option">
            <span class="v-type-opt">
                @if (option.shape) {
                    <svg [vShape]="option.shape" st="key"></svg>
                } @else {
                    <span class="v-type-opt__gap"></span>
                }
                <span class="v-type-opt__name">{{ option.name }}</span>
            </span>
        </ng-template>
    `,
    styles: `
        :host { display: flex; flex-direction: column; flex: 1 1 auto; min-height: 0; }
        .v-content { flex: 1 1 auto; min-height: 0; overflow: auto; padding: var(--kbq-size-l) var(--kbq-size-xxl) var(--kbq-size-xxl); }
        .v-kpis { display: flex; flex-wrap: wrap; gap: 48px; margin-top: var(--kbq-size-l); }
        .v-kpi__v {
            font-family: var(--kbq-typography-headline-font-family);
            font-size: var(--kbq-typography-headline-font-size);
            line-height: var(--kbq-typography-headline-line-height);
            font-weight: var(--kbq-typography-headline-font-weight);
            letter-spacing: var(--kbq-typography-headline-letter-spacing);
            font-variant-numeric: tabular-nums;
            color: var(--kbq-foreground-contrast);
        }
        .v-kpi__t { color: var(--kbq-foreground-contrast-secondary); }
        .v-chart { margin-top: var(--kbq-size-3xl); }
        .v-legend {
            display: grid;
            grid-template-columns: max-content minmax(0, 1fr);
            gap: var(--kbq-size-xs) var(--kbq-size-l);
            margin: var(--kbq-size-l) 0 0;
            font-size: var(--kbq-typography-text-compact-font-size);
            line-height: var(--kbq-typography-text-compact-line-height);
            color: var(--kbq-foreground-contrast-secondary);
        }
        .v-legend dt { color: var(--kbq-foreground-contrast); }
        .v-legend dd { display: flex; flex-wrap: wrap; gap: var(--kbq-size-xxs) 20px; margin: 0; }
        .v-key { display: inline-flex; align-items: center; gap: 6px; }
        /* The stem above a mark on the chart, in the neutral key colour. */
        .v-key__stem { width: 2px; height: 14px; border-radius: 1px; background: currentColor; }
        /* Block-level flex: an inline-flex box sits on the text baseline and makes the option taller. */
        .v-type-opt { display: flex; align-items: center; gap: var(--kbq-size-s); min-width: 0; }
        .v-type-opt__gap { width: 12px; flex: none; }
        .v-type-opt__name { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        .v-chart__empty { padding: var(--kbq-size-3xl) 0; }
        .v-source {
            margin: var(--kbq-size-3xl) 0 0;
            padding-top: var(--kbq-size-m);
            border-top: 1px solid var(--kbq-line-contrast-less);
            color: var(--kbq-foreground-contrast-tertiary);
            font-size: var(--kbq-typography-text-compact-font-size);
            line-height: var(--kbq-typography-text-compact-line-height);
        }
    `,
})
export class StatisticsComponent {
    private readonly api = inject(AdminConfigApi);
    private readonly router = inject(Router);
    private readonly route = inject(ActivatedRoute);
    private readonly query = toSignal(this.route.queryParamMap, { initialValue: this.route.snapshot.queryParamMap });

    protected readonly EMPTY = EMPTY;
    protected readonly dur = dur;
    protected readonly countText = (n: number) => plural(n, 'запуск', 'запуска', 'запусков');

    protected readonly period = computed<Period>(() => parsePeriod(this.query().get('period')));
    protected readonly types = computed(() => parseTypes(this.query().get('type')), { equal: sameList });
    /** Period other than the default, or any type: «Сбросить» brings both back to the defaults. */
    protected readonly filtered = computed(
        () => periodParam(this.period()) !== DEFAULT_PERIOD || this.types().length > 0,
    );
    /** Bumped after «Сбросить» and when task titles arrive, so the pipes are re-created (see pipeTrackKey). */
    private readonly pipeEpoch = signal(0);
    protected readonly pipeKey = pipeTrackKey;

    protected readonly tasks = signal<AdminTask[]>([]);
    protected readonly data = signal<StatRunsResponse | null>(null);
    protected readonly loading = signal(false);
    protected readonly error = signal<unknown>(null);
    /** Absolute range of the loaded data: fixed when the request is sent, so a preset does not drift. */
    protected readonly range = signal({ from: 0, to: 1 });

    protected readonly now = computed(() => {
        const at = this.data()?.generated_at;
        return at ? Math.min(Date.parse(at), Math.max(this.range().to, Date.now())) : Date.now();
    });
    protected readonly runs = computed(() => {
        const now = this.now();
        return (this.data()?.items ?? []).map((r) => toChartRun(r, now));
    });
    protected readonly stats = computed(() => kpis(this.runs()));

    /** Shapes by kind of task (see assignTaskShapes), stable across filters; «?» for questions. */
    protected readonly shapes = computed<Record<string, ShapeName>>(() => ({
        q: 'question',
        ...assignTaskShapes(this.tasks()),
    }));
    private readonly sortedTasks = computed(() => sortByName(this.tasks(), (t) => t.title));
    private readonly typeOptionTpl = viewChild<TemplateRef<unknown>>('typeOption');
    protected readonly keys = computed(() => {
        const types = this.types();
        const shapes = this.shapes();
        const q = { shapeKey: 'q', shape: 'question' as ShapeName, label: 'вопрос' };
        const tasks = this.sortedTasks().map((t) => ({ shapeKey: t.id, shape: shapes[t.id], label: lowerFirst(t.title) }));
        const all = [q, ...tasks];
        if (!types.length) return this.withMarks(all);
        const picked = all.filter((k) =>
            k === q ? types.includes('question') : types.includes('task') || types.includes(k.shapeKey),
        );
        // One picked type keeps its key even without marks; several list only the ones on the chart.
        return picked.length === 1 ? picked : this.withMarks(picked);
    });

    /** «Вопросы», then every task; ids from the URL that are not (yet) known keep the raw id as the name. */
    private readonly typeOptions = computed<TypeOption[]>(() => {
        const options: TypeOption[] = [
            { name: 'Вопросы', value: 'question', id: 'question', shape: 'question' },
            ...this.sortedTasks().map((t) => ({ name: t.title, value: t.id, id: t.id, shape: this.shapes()[t.id] ?? null })),
        ];
        const unknown = this.types().filter((v) => !options.some((o) => o.value === v));
        return [...options, ...unknown.map((v) => ({ name: v === 'task' ? 'Все задачи' : v, value: v, id: v, shape: null }))];
    });

    protected readonly templates = computed<KbqPipeTemplate[]>(() => [
        {
            name: PERIOD_PIPE,
            type: KbqPipeTypes.Datetime,
            values: PERIODS.map((p) => ({ name: p.name, start: p.start, end: null })),
            cleanable: false,
            removable: false,
            disabled: false,
        },
        {
            name: TYPE_PIPE,
            type: KbqPipeTypes.MultiSelect,
            values: this.typeOptions(),
            valueTemplate: this.typeOptionTpl(),
            compareWith: (a, b) => !!a && !!b && a.value === b.value,
            cleanable: true,
            removable: false,
            disabled: false,
        },
    ]);

    protected readonly filter = computed<KbqFilter>(() => {
        const epoch = this.pipeEpoch();
        const p = this.period();
        const types = this.types();
        const pipes: KbqPipe[] = [
            {
                name: PERIOD_PIPE,
                type: KbqPipeTypes.Datetime,
                value: 'preset' in p ? { name: p.preset.name, start: p.preset.start, end: null } : { start: p.from, end: p.to },
                cleanable: false,
                removable: false,
                disabled: false,
            },
            {
                name: TYPE_PIPE,
                type: KbqPipeTypes.MultiSelect,
                value: this.typeOptions().filter((o) => types.includes(o.value)),
                cleanable: true,
                removable: false,
                disabled: false,
            },
        ];
        return { name: '', pipes: withEpoch(pipes, epoch), readonly: false, disabled: false, changed: false, saved: false };
    });

    constructor() {
        this.api.tasks().then(
            (t) => {
                this.tasks.set(t);
                // A `?type=<task>` deep link: the chip was created with the raw id, re-create it with the title.
                if (this.types().length) this.pipeEpoch.update((n) => n + 1);
            },
            () => undefined, // type options degrade to «Вопросы»; the runs request reports errors
        );
        effect(() => {
            this.period();
            this.types();
            void this.load();
        });
    }

    private seq = 0;

    async load(): Promise<void> {
        const p = this.period();
        const types = this.types();
        const range = periodRange(p, Date.now());
        const seq = ++this.seq;
        this.loading.set(true);
        this.error.set(null);
        try {
            const res = await this.api.statRuns({
                from: new Date(range.from).toISOString(),
                to: new Date(range.to).toISOString(),
                type: typeParam(types),
            });
            if (seq !== this.seq) return;
            this.range.set(range);
            this.data.set({ ...res, items: runsByTypes(res.items, types) });
        } catch (err) {
            if (seq === this.seq) this.error.set(err);
        } finally {
            if (seq === this.seq) this.loading.set(false);
        }
    }

    protected onPipe(pipe: KbqPipe): void {
        if (pipe.name === PERIOD_PIPE) {
            const v = pipe.value as { name?: string; start?: string; end?: string } | null;
            const preset = v?.name ? PERIODS.find((p) => p.name === v.name) : undefined;
            if (preset) void this.navigate({ period: periodParam({ preset }) });
            else if (v?.start && v?.end) void this.navigate({ period: periodParam({ from: v.start, to: v.end }) });
        } else if (pipe.name === TYPE_PIPE) {
            const types = multiSelectValues(pipe.value);
            this.navigate({ type: types.length ? types.join(',') : null });
        }
    }

    /** «Сбросить»: the default period and every type; the pipes are re-created once the URL is cleared. */
    protected resetAll(): void {
        void this.navigate({ period: null, type: null }).then(() => this.pipeEpoch.update((n) => n + 1));
    }

    private withMarks<T extends { shapeKey: string }>(keys: T[]): T[] {
        const seen = new Set(this.runs().map((r) => r.shapeKey));
        return keys.filter((k) => seen.has(k.shapeKey));
    }

    private navigate(params: { period?: string | null; type?: string | null }): Promise<boolean> {
        return this.router.navigate([], { relativeTo: this.route, queryParams: params, queryParamsHandling: 'merge' });
    }
}

const sameList = (a: readonly string[], b: readonly string[]) => a.length === b.length && a.every((v, i) => v === b[i]);

const lowerFirst = (s: string) => (s && !/^[A-ZА-ЯЁ]{2}/.test(s) ? s[0].toLocaleLowerCase('ru') + s.slice(1) : s);
