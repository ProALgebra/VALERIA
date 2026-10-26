import { ChangeDetectionStrategy, Component, computed, DestroyRef, ElementRef, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { Router, RouterLink } from '@angular/router';
import { KbqAlertColors, KbqAlertModule, KbqAlertStyles } from '@koobiq/components/alert';
import { KbqBadgeColors, KbqBadgeModule } from '@koobiq/components/badge';
import { KbqButtonModule } from '@koobiq/components/button';
import { KbqComponentColors } from '@koobiq/components/core';
import { KbqFormFieldModule } from '@koobiq/components/form-field';
import { FieldErrorDirective } from '../../../shared/field-error/field-error.directive';
import { KbqIconModule } from '@koobiq/components/icon';
import { KbqInputModule } from '@koobiq/components/input';
import { KbqLinkModule } from '@koobiq/components/link';
import { KbqSelectModule } from '@koobiq/components/select';
import { KbqToolTipModule } from '@koobiq/components/tooltip';
import { DirtyAware, DirtyFormService } from '../../../core/guards/dirty.guard';
import { NotifyService } from '../../../core/notify/notify.service';
import { PageStateComponent } from '../../../shared/page-state/page-state.component';
import { ScreenHeaderComponent } from '../../../shell/screen-header/screen-header.component';
import { AdminConfigApi, AdminTask, QueueOverview } from '../config-shared/admin-config.api';
import { FormAlertsComponent, FormPageComponent, FrowComponent, FsecComponent } from '../config-shared/form-layout';
import { SyncSubmit } from '../config-shared/sync-submit';
import { ADMIN_GRID } from '../shared/admin-grid';
import { archiveBlock, fromOverview, planRequests, QueueFormRow, QueueFormValue, QueueRequest, usedBy, validateQueues, visibleRows } from './queue-form';

interface QueueDraft {
    name: string;
    priority: number | null;
    max_workers: number | null;
}

/** A row of the queues table: a queue, or the queue being added. */
interface QueueGridRow {
    key: string;
    row: QueueFormRow | null;
    draft: QueueDraft | null;
}

const DRAFT_ROW_KEY = 'draft';

/** «Очереди и лимиты» edit form (prototype `queueEdit`): every change is applied together on «Сохранить». */
@Component({
    selector: 'v-queue-edit',
    imports: [
        FormsModule,
        RouterLink,
        ScreenHeaderComponent,
        PageStateComponent,
        FormPageComponent,
        FsecComponent,
        FrowComponent,
        FormAlertsComponent,
        KbqAlertModule,
        KbqBadgeModule,
        KbqButtonModule,
        KbqFormFieldModule,
        FieldErrorDirective,
        KbqIconModule,
        KbqInputModule,
        KbqLinkModule,
        KbqSelectModule,
        KbqToolTipModule,
        ADMIN_GRID,
    ],
    changeDetection: ChangeDetectionStrategy.OnPush,
    template: `
        <v-screen-header title="Очереди и лимиты" />
        <v-page-state [loading]="loading()" [error]="loadError()" (retry)="load()">
            @if (form(); as v) {
                <v-form-page [wide]="true">
                    @if (blocked(); as b) {
                        <kbq-alert [compact]="true" class="v-blocked" [alertColor]="alertColors.Error" [alertStyle]="alertStyles.Colored" role="alert">
                            <i kbq-icon="kbq-circle-exclamation_16" color="error"></i>
                            <div kbq-alert-title>{{ b.title }}</div>
                            {{ b.text }}
                            <a kbq-button kbq-alert-control [color]="colors.ContrastFade" routerLink="/admin/tasks">Настройка задач</a>
                        </kbq-alert>
                    }
                    <v-form-alerts [submit]="submit" (overwrite)="overwrite()" />

                    <fieldset vFsec="Общий лимит">
                        <v-frow label="Одновременно" for="q-limit">
                            <div class="v-inline">
                                <kbq-form-field class="v-num">
                                    <input
                                        id="q-limit"
                                        kbqNumberInput
                                        [integer]="true"
                                        [withThousandSeparator]="false"
                                        [min]="1"
                                        [max]="hardLimit()"
                                        [ngModel]="v.useEnv ? envLimit() : v.limit"
                                        (ngModelChange)="setLimit($event)"
                                    />
                                </kbq-form-field>
                                <span>сессий</span>
                                @if (!v.useEnv) {
                                    <button type="button" kbq-button [color]="colors.Contrast" kbqStyle="transparent" (click)="useEnv()">
                                        <i kbq-icon="kbq-arrow-rotate-left_16"></i>
                                        Вернуть {{ envLimit() }} из конфигурации
                                    </button>
                                }
                            </div>
                            @if (shownErrors()['limit']; as e) {
                                <kbq-hint color="error">{{ e }}</kbq-hint>
                            }
                            <kbq-hint>От 1 до {{ hardLimit() }}. Сессии, которые ждут ответа пользователя, слот не занимают.</kbq-hint>
                        </v-frow>
                    </fieldset>

                    <fieldset vFsec="Очереди">
                        <kbq-hint class="v-qhint">
                            Лимит очереди не отменяет общий лимит и лимиты пользователей.
                        </kbq-hint>
                        <!-- One grid row per queue, plus the new queue's row («Добавить очередь») at the end. -->
                        <v-admin-grid editable [rows]="gridRows()" [rowId]="gridRowId">
                            <ng-template vCol="name" header="Название" kind="control" [flex]="1" [minWidth]="200" let-g>
                                @if (g.row; as r) {
                                    @if (r.id) {
                                        <span class="v-qname">
                                            {{ r.name }}
                                            @if (r.is_system) {
                                                <kbq-badge [compact]="true" [badgeColor]="badgeColors.FadeContrast">системная</kbq-badge>
                                            }
                                        </span>
                                    } @else {
                                        <kbq-form-field>
                                            <input kbqInput aria-label="Название" [vFieldError]="shownErrors()[r.key + ':name']" [ngModel]="r.name" (ngModelChange)="patchRow(r.key, { name: $event })" />
                                            <kbq-error role="alert">{{ shownErrors()[r.key + ':name'] }}</kbq-error>
                                        </kbq-form-field>
                                    }
                                } @else if (g.draft; as d) {
                                    <kbq-form-field class="v-addrow">
                                        <input
                                            kbqInput
                                            placeholder="Название"
                                            aria-label="Название новой очереди"
                                            [ngModel]="d.name"
                                            (ngModelChange)="patchDraft({ name: $event })"
                                            (keydown.enter)="addQueue()"
                                        />
                                    </kbq-form-field>
                                }
                            </ng-template>
                            <ng-template vCol="priority" header="Приоритет" kind="control" [width]="136" let-g>
                                @if (g.row; as r) {
                                    <kbq-form-field class="v-num">
                                        <input
                                            kbqNumberInput
                                            [integer]="true"
                                            [withThousandSeparator]="false"
                                            [attr.aria-label]="'Приоритет: ' + r.name"
                                            [ngModel]="r.priority"
                                            (ngModelChange)="patchRow(r.key, { priority: num($event) })"
                                        />
                                    </kbq-form-field>
                                    @if (shownErrors()[r.key + ':priority']; as e) {
                                        <kbq-hint color="error">{{ e }}</kbq-hint>
                                    }
                                } @else if (g.draft; as d) {
                                    <kbq-form-field class="v-num">
                                        <input
                                            kbqNumberInput
                                            [integer]="true"
                                            [withThousandSeparator]="false"
                                            placeholder="0"
                                            aria-label="Приоритет новой очереди"
                                            [ngModel]="d.priority"
                                            (ngModelChange)="patchDraft({ priority: num($event) })"
                                        />
                                    </kbq-form-field>
                                }
                            </ng-template>
                            <ng-template vCol="max" header="Воркеров, не больше" kind="control" [width]="184" let-g>
                                @if (g.row; as r) {
                                    <kbq-form-field class="v-num">
                                        <input
                                            kbqNumberInput
                                            [integer]="true"
                                            [withThousandSeparator]="false"
                                            [min]="1"
                                            [max]="hardLimit()"
                                            [attr.aria-label]="'Воркеров, не больше: ' + r.name"
                                            [ngModel]="r.max_workers"
                                            (ngModelChange)="patchRow(r.key, { max_workers: num($event) })"
                                        />
                                    </kbq-form-field>
                                    @if (shownErrors()[r.key + ':max_workers']; as e) {
                                        <kbq-hint color="error">{{ e }}</kbq-hint>
                                    }
                                } @else if (g.draft; as d) {
                                    <kbq-form-field class="v-num">
                                        <input
                                            kbqNumberInput
                                            [integer]="true"
                                            [withThousandSeparator]="false"
                                            [min]="1"
                                            [max]="hardLimit()"
                                            aria-label="Воркеров, не больше, у новой очереди"
                                            [ngModel]="d.max_workers"
                                            (ngModelChange)="patchDraft({ max_workers: num($event) })"
                                        />
                                    </kbq-form-field>
                                }
                            </ng-template>
                            <ng-template vCol="now" header="Сейчас" kind="nowrap" let-g>
                                @if (g.row?.id) {
                                    <span class="v-secondary">{{ g.row.active }} в работе · {{ g.row.queued }} ждут</span>
                                }
                            </ng-template>
                            <ng-template vCol="actions" header="Действия" kind="action" [width]="200" let-g>
                                <span class="v-actions">
                                    @if (g.row; as r) {
                                        @if (r.is_system) {
                                            <span kbqTooltip="Основную очередь архивировать нельзя" kbqPlacement="left">
                                                <button type="button" kbq-button [color]="colors.ContrastFade" disabled>Архивировать</button>
                                            </span>
                                        } @else {
                                            <button type="button" kbq-button [color]="colors.ContrastFade" (click)="archive(r)">Архивировать</button>
                                        }
                                    } @else if (g.draft; as d) {
                                        <span kbqTooltip="Укажите название" kbqPlacement="left" [kbqTooltipDisabled]="!!d.name.trim()">
                                            <button type="button" kbq-button [color]="colors.Contrast" [disabled]="!d.name.trim()" (click)="addQueue()">
                                                Добавить
                                            </button>
                                        </span>
                                        <button type="button" kbq-button [color]="colors.ContrastFade" (click)="draft.set(null)">Отмена</button>
                                    }
                                </span>
                            </ng-template>
                        </v-admin-grid>
                        @if (!draft()) {
                            <div class="v-addq">
                                <button type="button" kbq-button [color]="colors.Contrast" kbqStyle="transparent" (click)="startAdd()">
                                    <i kbq-icon="kbq-plus_16"></i>
                                    Добавить очередь
                                </button>
                            </div>
                        }
                    </fieldset>

                    <fieldset vFsec="Куда попадают запросы">
                        <v-frow label="Вопросы">
                            <div class="v-inline">
                                <kbq-form-field class="v-select">
                                    <kbq-select aria-label="Очередь для вопросов" [value]="v.consultQueue" (valueChange)="patch({ consultQueue: $event })">
                                        @for (q of routable(); track q.id) {
                                            <kbq-option [value]="q.id">{{ q.name }}</kbq-option>
                                        }
                                    </kbq-select>
                                </kbq-form-field>
                                <kbq-form-field class="v-select">
                                    <kbq-select aria-label="Модель для вопросов" [value]="v.consultModel" (valueChange)="patch({ consultModel: $event })">
                                        @for (m of models(); track m) {
                                            <kbq-option [value]="m">{{ m }}</kbq-option>
                                        }
                                    </kbq-select>
                                </kbq-form-field>
                            </div>
                            <kbq-hint>Режим всегда Build, только чтение. Новая модель действует со следующего вопроса.</kbq-hint>
                        </v-frow>
                        <v-frow label="Работа в папке">
                            <kbq-form-field class="v-select">
                                <kbq-select aria-label="Очередь для работы в папке" [value]="v.chatQueue" (valueChange)="patch({ chatQueue: $event })">
                                    @for (q of routable(); track q.id) {
                                        <kbq-option [value]="q.id">{{ q.name }}</kbq-option>
                                    }
                                </kbq-select>
                            </kbq-form-field>
                            <kbq-hint>Модель пользователь выбирает сам, когда начинает работу.</kbq-hint>
                        </v-frow>
                        <v-frow label="Задачи">
                            <div class="v-secondary v-pt6">
                                Очередь и модель задаются в каждой задаче.
                                <a kbq-link routerLink="/admin/tasks">Настройка задач</a>
                            </div>
                        </v-frow>
                    </fieldset>

                    <ng-container vFootMain>
                        <button type="button" kbq-button [color]="colors.Contrast" [disabled]="submit.pending()" (click)="save()">Сохранить</button>
                        <a kbq-button [color]="colors.ContrastFade" routerLink="/admin/queue/settings">Отмена</a>
                    </ng-container>
                </v-form-page>
            }
        </v-page-state>
    `,
    styles: [
        `
        :host { display: flex; flex-direction: column; flex: 1 1 auto; min-height: 0; }
        .v-inline { display: flex; align-items: center; gap: var(--kbq-size-s); flex-wrap: wrap; }
        .v-num { width: 96px; }
        .v-select { width: 260px; }
        /* A saved queue's name sits on the line of the fields next to it. */
        .v-qname { display: flex; align-items: center; gap: var(--kbq-size-xs); min-height: var(--kbq-form-field-size-height); }
        .v-qhint { margin-top: calc(-1 * var(--kbq-size-xxs)); }
        .v-secondary { color: var(--kbq-foreground-contrast-secondary); }
        .v-actions { display: flex; gap: var(--kbq-size-xs); white-space: nowrap; }
        .v-pt6 { padding-top: var(--kbq-size-xs); }
        /* Prototype: «Добавить очередь» spans the table width, label centred, 8px below the table. */
        .v-addq { margin-top: var(--kbq-size-s); }
        .v-addq > .kbq-button { width: 100%; justify-content: center; }    `,
    ],
})
export class QueueEditComponent implements DirtyAware {
    private readonly api = inject(AdminConfigApi);
    private readonly router = inject(Router);
    private readonly notify = inject(NotifyService);
    private readonly host: ElementRef<HTMLElement> = inject(ElementRef);

    protected readonly colors = KbqComponentColors;
    protected readonly badgeColors = KbqBadgeColors;
    protected readonly alertColors = KbqAlertColors;
    protected readonly alertStyles = KbqAlertStyles;
    protected readonly submit = new SyncSubmit();

    protected readonly loading = signal(true);
    protected readonly loadError = signal<unknown>(null);
    private readonly overview = signal<QueueOverview | null>(null);
    private readonly tasks = signal<AdminTask[]>([]);
    private readonly modelIds = signal<string[]>([]);
    private readonly initial = signal<QueueFormValue | null>(null);
    protected readonly form = signal<QueueFormValue | null>(null);
    protected readonly draft = signal<QueueDraft | null>(null);
    protected readonly blocked = signal<{ title: string; text: string } | null>(null);
    private readonly submitted = signal(false);
    private newSeq = 0;

    protected readonly hardLimit = computed(() => this.overview()?.hard_limit ?? 1);
    protected readonly envLimit = computed(() => this.overview()?.env_limit ?? 1);
    protected readonly rows = computed(() => (this.form() ? visibleRows(this.form()!) : []));
    /** The table: the queues, then the row of the queue being added. */
    protected readonly gridRows = computed<QueueGridRow[]>(() => {
        const list: QueueGridRow[] = this.rows().map((row) => ({ key: row.key, row, draft: null }));
        const draft = this.draft();
        if (draft) list.push({ key: DRAFT_ROW_KEY, row: null, draft });
        return list;
    });
    protected readonly gridRowId = (g: QueueGridRow): string => g.key;
    /** Saved queues can take requests; a queue added here has to be saved first. */
    protected readonly routable = computed(() => this.rows().filter((r) => r.id) as (QueueFormRow & { id: string })[]);
    protected readonly models = computed(() => {
        const ids = this.modelIds();
        const cur = this.form()?.consultModel;
        return cur && !ids.includes(cur) ? [cur, ...ids] : ids;
    });
    private readonly errors = computed(() => (this.form() ? validateQueues(this.form()!, this.hardLimit()) : {}));
    protected readonly shownErrors = computed(() => (this.submitted() ? this.errors() : {}));

    constructor() {
        const untrack = inject(DirtyFormService).track(() => this.isDirty());
        inject(DestroyRef).onDestroy(untrack);
        void this.load();
    }

    isDirty(): boolean {
        const v = this.form();
        const d = this.draft();
        return !!v && (JSON.stringify(v) !== JSON.stringify(this.initial()) || !!d?.name.trim());
    }

    async load(): Promise<void> {
        this.loading.set(true);
        this.loadError.set(null);
        try {
            const [ov, consult, tasks, options] = await Promise.all([this.api.queue(), this.api.consultations(), this.api.tasks(), this.api.options()]);
            this.overview.set(ov);
            this.tasks.set(tasks);
            this.modelIds.set(options.models.map((m) => m.id));
            const v = fromOverview(ov, consult);
            this.initial.set(structuredClone(v));
            this.form.set(v);
        } catch (err) {
            this.loadError.set(err);
        } finally {
            this.loading.set(false);
        }
    }

    protected num(v: unknown): number | null {
        return typeof v === 'number' && Number.isFinite(v) ? v : null;
    }

    protected patch(p: Partial<QueueFormValue>): void {
        this.form.update((v) => (v ? { ...v, ...p } : v));
    }

    protected patchRow(key: string, p: Partial<QueueFormRow>): void {
        this.form.update((v) => (v ? { ...v, rows: v.rows.map((r) => (r.key === key ? { ...r, ...p } : r)) } : v));
    }

    protected setLimit(value: unknown): void {
        this.patch({ limit: this.num(value), useEnv: false });
    }

    protected useEnv(): void {
        this.patch({ useEnv: true, limit: this.envLimit() });
    }

    protected startAdd(): void {
        this.draft.set({ name: '', priority: null, max_workers: 1 });
        this.focusAddRow();
    }

    /** The grid draws the new row a frame or two later: wait for its name field. */
    private focusAddRow(frames = 10): void {
        requestAnimationFrame(() => {
            const input = this.host.nativeElement.querySelector<HTMLInputElement>('.v-addrow input');
            if (input) input.focus();
            else if (frames > 0) this.focusAddRow(frames - 1);
        });
    }

    protected patchDraft(p: Partial<QueueDraft>): void {
        this.draft.update((d) => (d ? { ...d, ...p } : d));
    }

    /** Adds the row to the table; the queue is created on «Сохранить». */
    protected addQueue(): void {
        const d = this.draft();
        if (!d || !d.name.trim()) return;
        const row: QueueFormRow = {
            id: null,
            key: `new-${++this.newSeq}`,
            name: d.name,
            priority: d.priority,
            max_workers: d.max_workers,
            is_system: false,
            updated_at: null,
            active: 0,
            queued: 0,
        };
        this.form.update((v) => (v ? { ...v, rows: [...v.rows, row] } : v));
        this.draft.set(null);
    }

    /** A queue that is used or busy can't go: explain why above the form instead of failing on save. */
    protected archive(r: QueueFormRow): void {
        const v = this.form()!;
        if (!r.id) {
            this.form.set({ ...v, rows: v.rows.filter((o) => o.key !== r.key) });
            return;
        }
        const used = usedBy(r.id, { consultQueue: v.consultQueue, chatQueue: v.chatQueue, taskQueues: this.tasks().map((t) => t.queue_id) });
        const block = archiveBlock(r, used);
        this.blocked.set(block);
        if (block) {
            this.host.nativeElement.querySelector('v-form-page')?.scrollTo({ top: 0 });
            return;
        }
        this.form.set({ ...v, archived: [...v.archived, r.id] });
    }

    protected async save(): Promise<void> {
        this.submitted.set(true);
        this.blocked.set(null);
        if (Object.keys(this.errors()).length) {
            setTimeout(() => this.host.nativeElement.querySelector<HTMLElement>('.kbq-error, kbq-hint[color="error"]')?.scrollIntoView({ block: 'center' }));
            return;
        }
        const res = await this.submit.run(() => this.apply());
        if (!res.ok) return;
        this.notify.success('Настройки очередей сохранены');
        void this.router.navigateByUrl('/admin/queue/settings');
    }

    /** Runs the planned requests one by one; each success moves `initial`, so a retry only sends what is left. */
    private async apply(): Promise<void> {
        for (const req of planRequests(this.initial()!, this.form()!)) {
            await this.run(req);
        }
    }

    private async run(req: QueueRequest): Promise<void> {
        const env = this.envLimit();
        switch (req.kind) {
            case 'concurrency':
                await this.api.setConcurrency(req.value);
                return this.settle((v) => ({ ...v, useEnv: false, limit: req.value }));
            case 'concurrency-reset':
                await this.api.resetConcurrency();
                return this.settle((v) => ({ ...v, useEnv: true, limit: env }));
            case 'create': {
                const q = await this.api.createQueue(req.body);
                const saved = (r: QueueFormRow): QueueFormRow =>
                    r.key === req.key ? { ...r, id: q.id, name: q.name, priority: q.priority, max_workers: q.max_workers, updated_at: q.updated_at } : r;
                this.form.update((v) => v && { ...v, rows: v.rows.map(saved) });
                this.initial.update((v) => v && { ...v, rows: [...v.rows, saved(this.form()!.rows.find((r) => r.key === req.key)!)] });
                return;
            }
            case 'patch': {
                const q = await this.api.patchQueue(req.id, req.body);
                const saved = (r: QueueFormRow): QueueFormRow => (r.id === q.id ? { ...r, name: q.name, priority: q.priority, max_workers: q.max_workers, updated_at: q.updated_at } : r);
                this.form.update((v) => v && { ...v, rows: v.rows.map(saved) });
                this.initial.update((v) => v && { ...v, rows: v.rows.map(saved) });
                return;
            }
            case 'consultations':
                await this.api.setConsultations(req.body);
                return this.settle((v) => ({ ...v, consultQueue: req.body.queue_id, consultModel: req.body.model }));
            case 'chat-queue':
                await this.api.setDefaultChatQueue(req.queue_id);
                return this.settle((v) => ({ ...v, chatQueue: req.queue_id }));
            case 'archive':
                await this.api.archiveQueue(req.id);
                this.form.update((v) => v && { ...v, rows: v.rows.filter((r) => r.id !== req.id), archived: v.archived.filter((id) => id !== req.id) });
                this.initial.update((v) => v && { ...v, rows: v.rows.filter((r) => r.id !== req.id) });
                return;
        }
    }

    private settle(fn: (v: QueueFormValue) => QueueFormValue): void {
        this.initial.update((v) => v && fn(v));
    }

    /** «Перезаписать»: take the server's current `updated_at` for every saved queue and save again. */
    protected async overwrite(): Promise<void> {
        const res = await this.submit.run(async () => {
            const ov = await this.api.queue();
            const stamp = (r: QueueFormRow): QueueFormRow => {
                const s = ov.queues.find((q) => q.id === r.id);
                return s ? { ...r, updated_at: s.updated_at } : r;
            };
            this.form.update((v) => v && { ...v, rows: v.rows.map(stamp) });
            this.initial.update((v) => v && { ...v, rows: v.rows.map(stamp) });
            await this.apply();
        });
        if (!res.ok) return;
        this.notify.success('Настройки очередей сохранены');
        void this.router.navigateByUrl('/admin/queue/settings');
    }
}
