import { ChangeDetectionStrategy, Component, computed, DestroyRef, ElementRef, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { ActivatedRoute, Router, RouterLink } from '@angular/router';
import { KbqBadgeColors, KbqBadgeModule } from '@koobiq/components/badge';
import { KbqButtonModule } from '@koobiq/components/button';
import { KbqButtonToggleModule } from '@koobiq/components/button-toggle';
import { createSearchPredicate, KbqComponentColors } from '@koobiq/components/core';
import { KbqFormFieldModule } from '@koobiq/components/form-field';
import { FieldErrorDirective } from '../../../shared/field-error/field-error.directive';
import { KbqIconModule } from '@koobiq/components/icon';
import { KbqInputModule } from '@koobiq/components/input';
import { KbqRadioModule } from '@koobiq/components/radio';
import { KbqSelectModule } from '@koobiq/components/select';
import { KbqTagsModule } from '@koobiq/components/tags';
import { KbqTextareaModule } from '@koobiq/components/textarea';
import { KbqToggleModule } from '@koobiq/components/toggle';
import { DirtyAware, DirtyFormService } from '../../../core/guards/dirty.guard';
import { NotifyService } from '../../../core/notify/notify.service';
import { PageStateComponent } from '../../../shared/page-state/page-state.component';
import { ScreenHeaderComponent } from '../../../shell/screen-header/screen-header.component';
import { AdminConfigApi, AdminTask, AdminUser, isOn, Options, QueueRow } from '../config-shared/admin-config.api';
import { plural } from '../config-shared/format';
import { FormAlertsComponent, FormPageComponent, FrowComponent, FsecComponent } from '../config-shared/form-layout';
import { sortByName } from '../config-shared/names';
import { SyncSubmit } from '../config-shared/sync-submit';
import { emptyTask, fromTask, sameTask, TaskFormErrors, TaskFormValue, toPayload, validateTask } from './task-form';

type Action = 'draft' | 'publish' | 'save' | 'unpublish';

const users = (n: number) => plural(n, 'пользователь', 'пользователя', 'пользователей');

/** Full-page task form (prototype `ataskView` edit/published), for `/admin/tasks/new` and `/admin/tasks/:id`. */
@Component({
    selector: 'v-task-form',
    imports: [
        FormsModule,
        RouterLink,
        ScreenHeaderComponent,
        PageStateComponent,
        FormPageComponent,
        FsecComponent,
        FrowComponent,
        FormAlertsComponent,
        KbqBadgeModule,
        KbqButtonModule,
        KbqButtonToggleModule,
        KbqFormFieldModule,
        FieldErrorDirective,
        KbqIconModule,
        KbqInputModule,
        KbqRadioModule,
        KbqSelectModule,
        KbqTagsModule,
        KbqTextareaModule,
        KbqToggleModule,
    ],
    changeDetection: ChangeDetectionStrategy.OnPush,
    template: `
        <v-screen-header [title]="headerTitle()">
            <ng-container vHeaderAfterTitle>
                @if (form()) {
                    <kbq-badge [badgeColor]="published() ? badgeColors.FadeSuccess : badgeColors.FadeContrast">
                        {{ published() ? 'Опубликована' : 'Черновик' }}
                    </kbq-badge>
                }
            </ng-container>
        </v-screen-header>
        <v-page-state [loading]="loading()" [error]="loadError()" (retry)="load()">
            @if (form(); as v) {
                <v-form-page>
                    <v-form-alerts [submit]="submit" [users]="userList()" [recreatable]="true" (overwrite)="overwrite()" (recreate)="recreate()" />

                    <fieldset vFsec="Что видит пользователь">
                        <v-frow label="Название" for="t-title" [required]="true">
                            <kbq-form-field>
                                <input id="t-title" [vFieldError]="shown().title" kbqInput [ngModel]="v.title" (ngModelChange)="patch({ title: $event })" />
                                <kbq-error role="alert">{{ shown().title }}</kbq-error>
                            </kbq-form-field>
                        </v-frow>
                        <v-frow label="Описание" for="t-desc">
                            <kbq-form-field>
                                <textarea id="t-desc" kbqTextarea rows="2" [canGrow]="false" [ngModel]="v.description" (ngModelChange)="patch({ description: $event })"></textarea>
                            </kbq-form-field>
                            <kbq-hint>Пользователь видит название, описание и статусы запуска. Инструкцию и ход работы он не видит.</kbq-hint>
                        </v-frow>
                        <v-frow label="Подсказка к дополнению" for="t-hint">
                            <kbq-form-field>
                                <input id="t-hint" [vFieldError]="shown().input_hint" kbqInput [ngModel]="v.input_hint" (ngModelChange)="patch({ input_hint: $event })" />
                                <kbq-error role="alert">{{ shown().input_hint }}</kbq-error>
                            </kbq-form-field>
                        </v-frow>
                    </fieldset>

                    <fieldset vFsec="Инструкция агенту">
                        <v-frow label="Инструкция" for="t-content" [required]="true">
                            <kbq-form-field>
                                <textarea
                                    id="t-content" [vFieldError]="shown().content"
                                    class="v-mono"
                                    kbqTextarea
                                    rows="7"
                                    [canGrow]="false"
                                    [ngModel]="v.content"
                                    (ngModelChange)="patch({ content: $event })"
                                ></textarea>
                                <kbq-error role="alert">{{ shown().content }}</kbq-error>
                            </kbq-form-field>
                            <kbq-hint>Пользователь её не видит. Его дополнение добавляется в конец.</kbq-hint>
                        </v-frow>
                    </fieldset>

                    <fieldset vFsec="Исполнение">
                        <v-frow label="Режим">
                            <kbq-button-toggle-group class="v-toggles" aria-label="Режим" [ngModel]="v.agent" (ngModelChange)="patch({ agent: $event })">
                                @for (a of agents(); track a) {
                                    <kbq-button-toggle [value]="a">{{ agentLabel(a) }}</kbq-button-toggle>
                                }
                            </kbq-button-toggle-group>
                        </v-frow>
                        <v-frow label="Модель">
                            <kbq-form-field class="v-w320">
                                <kbq-select aria-label="Модель" [value]="v.model" (valueChange)="patch({ model: $event })">
                                    @for (m of models(); track m) {
                                        <kbq-option [value]="m">{{ m }}</kbq-option>
                                    }
                                </kbq-select>
                            </kbq-form-field>
                        </v-frow>
                        <v-frow label="Вариант модели" for="t-variant">
                            <kbq-form-field class="v-w320">
                                <input
                                    id="t-variant"
                                    kbqInput
                                    placeholder="Необязательно, например high"
                                    [ngModel]="v.model_variant"
                                    (ngModelChange)="patch({ model_variant: $event })"
                                />
                            </kbq-form-field>
                        </v-frow>
                        <v-frow label="Очередь">
                            <kbq-form-field class="v-w320">
                                <kbq-select aria-label="Очередь" [value]="v.queue_id" (valueChange)="patch({ queue_id: $event })">
                                    @for (q of queues(); track q.id) {
                                        <kbq-option [value]="q.id">{{ q.name }}</kbq-option>
                                    }
                                </kbq-select>
                            </kbq-form-field>
                        </v-frow>
                        <v-frow>
                            <kbq-toggle [ngModel]="v.plan_first" (ngModelChange)="patch({ plan_first: $event })">Сначала план, затем выполнение</kbq-toggle>
                        </v-frow>
                    </fieldset>

                    <fieldset vFsec="Доступ">
                        <v-frow label="Кто видит">
                            <kbq-radio-group aria-label="Кто видит" [ngModel]="v.visibility" (ngModelChange)="patch({ visibility: $event })">
                                <kbq-radio-button value="all">Все пользователи</kbq-radio-button>
                                <kbq-radio-button value="selected">Выбранные</kbq-radio-button>
                            </kbq-radio-group>
                            @if (v.visibility === 'selected') {
                                <kbq-form-field class="v-tags">
                                    <kbq-select
                                        multiple
                                        aria-label="Пользователи"
                                        placeholder="Добавить…"
                                        [value]="v.user_ids"
                                        (valueChange)="patch({ user_ids: $event })"
                                    >
                                        <kbq-form-field noBorders kbqSelectSearch>
                                            <i kbq-icon="kbq-magnifying-glass_16" kbqPrefix></i>
                                            <input
                                                kbqInput
                                                type="text"
                                                aria-label="Поиск пользователей"
                                                [ngModel]="audienceQuery()"
                                                (ngModelChange)="audienceQuery.set($event ?? '')"
                                            />
                                            <kbq-cleaner />
                                        </kbq-form-field>
                                        <div kbq-select-search-empty-result>Ничего не найдено</div>
                                        @for (u of audienceOptions(); track u.id) {
                                            <kbq-option [value]="u.id">{{ u.login }}</kbq-option>
                                        }
                                    </kbq-select>
                                </kbq-form-field>
                            }
                        </v-frow>
                    </fieldset>

                    <fieldset vFsec="Цепочка">
                        <v-frow label="Затем запустить">
                            <kbq-form-field class="v-tags">
                                <kbq-select
                                    multiple
                                    aria-label="Затем запустить"
                                    placeholder="Добавить задачу…"
                                    [value]="v.next_task_ids"
                                    (valueChange)="patch({ next_task_ids: $event })"
                                >
                                    @for (t of nextOptions(); track t.id) {
                                        <kbq-option [value]="t.id">{{ t.title }}</kbq-option>
                                    }
                                </kbq-select>
                            </kbq-form-field>
                            <kbq-hint>
                                Следующие задачи запускаются от имени того же пользователя после успешного завершения. Если задача упала или её отменили,
                                цепочка останавливается.
                            </kbq-hint>
                        </v-frow>
                    </fieldset>

                    <ng-container vFootMain>
                        @if (published()) {
                            <button type="button" kbq-button [color]="colors.Contrast" [disabled]="submit.pending()" (click)="act('save')">Сохранить</button>
                        } @else {
                            <button type="button" kbq-button [color]="colors.Contrast" [disabled]="submit.pending()" (click)="act('publish')">
                                <i kbq-icon="kbq-arrow-up-from-line_16"></i>
                                Опубликовать
                            </button>
                            <button type="button" kbq-button [color]="colors.ContrastFade" [disabled]="submit.pending()" (click)="act('draft')">
                                Сохранить черновик
                            </button>
                        }
                        <a kbq-button [color]="colors.ContrastFade" routerLink="/admin/tasks">Отмена</a>
                    </ng-container>
                    <ng-container vFootExtra>
                        @if (published()) {
                            <button type="button" kbq-button [color]="colors.ContrastFade" [disabled]="submit.pending()" (click)="act('unpublish')">
                                Снять с публикации
                            </button>
                        }
                    </ng-container>
                </v-form-page>
            }
        </v-page-state>
    `,
    styles: `
        :host { display: flex; flex-direction: column; flex: 1 1 auto; min-height: 0; }
        .v-w320 { width: 320px; }
        .v-tags { max-width: 560px; }
        .v-toggles { align-self: flex-start; }
        .v-mono { font-family: var(--kbq-typography-mono-normal-font-family); }
    `,
})
export class TaskFormComponent implements DirtyAware {
    private readonly api = inject(AdminConfigApi);
    private readonly router = inject(Router);
    private readonly notify = inject(NotifyService);
    private readonly host: ElementRef<HTMLElement> = inject(ElementRef);
    private readonly id: string | null = inject(ActivatedRoute).snapshot.paramMap.get('id');

    protected readonly colors = KbqComponentColors;
    protected readonly badgeColors = KbqBadgeColors;
    protected readonly submit = new SyncSubmit();

    protected readonly loading = signal(true);
    protected readonly loadError = signal<unknown>(null);
    private readonly task = signal<AdminTask | null>(null);
    private readonly allTasks = signal<AdminTask[]>([]);
    protected readonly userList = signal<AdminUser[]>([]);
    /** Search query inside the «Кто видит» user select. */
    protected readonly audienceQuery = signal('');
    protected readonly audienceOptions = computed(() => {
        const matches = createSearchPredicate(this.audienceQuery());
        return this.userList().filter((u) => matches(u.login));
    });
    private readonly options = signal<Options | null>(null);
    protected readonly queues = signal<QueueRow[]>([]);
    private readonly initial = signal<TaskFormValue | null>(null);
    protected readonly form = signal<TaskFormValue | null>(null);
    private readonly submitted = signal(false);
    /** The last attempted action, repeated by «Перезаписать» and «Создать объект». */
    private lastAction: Action = 'draft';
    private leaving = false;

    protected readonly published = computed(() => isOn(this.task()?.is_published));
    protected readonly headerTitle = computed(() => this.task()?.title ?? 'Новая задача');
    protected readonly agents = computed(() => {
        // Plan before Build, as in the prototype; any other agent from the server follows.
        const rank = (a: string) => ['plan', 'build'].indexOf(a) + 1 || 3;
        const list = [...(this.options()?.agents ?? ['plan', 'build'])].sort((a, b) => rank(a) - rank(b));
        const cur = this.form()?.agent;
        return cur && !list.includes(cur) ? [...list, cur] : list;
    });
    protected readonly models = computed(() => {
        const ids = (this.options()?.models ?? []).map((m) => m.id);
        const cur = this.form()?.model;
        return cur && !ids.includes(cur) ? [cur, ...ids] : ids;
    });
    protected readonly nextOptions = computed(() => sortByName(this.allTasks().filter((t) => t.id !== this.id), (t) => t.title));
    private readonly errors = computed<TaskFormErrors>(() => {
        const v = this.form();
        if (!v) return {};
        return validateTask(v, this.allTasks().filter((t) => t.id !== this.id).map((t) => t.title));
    });
    protected readonly shown = computed<TaskFormErrors>(() => (this.submitted() ? this.errors() : {}));

    constructor() {
        const untrack = inject(DirtyFormService).track(() => this.isDirty());
        inject(DestroyRef).onDestroy(untrack);
        void this.load();
    }

    isDirty(): boolean {
        const v = this.form();
        const i = this.initial();
        return !this.leaving && !!v && !!i && !sameTask(v, i);
    }

    async load(): Promise<void> {
        this.loading.set(true);
        this.loadError.set(null);
        try {
            // No single-task GET in the API: the form reads the list.
            const [tasks, userList, options, ov] = await Promise.all([this.api.tasks(), this.api.users(), this.api.options(), this.api.queue()]);
            this.allTasks.set(tasks);
            this.userList.set(sortByName(userList, (u) => u.login));
            this.options.set(options);
            this.queues.set(ov.queues.filter((q) => q.status === 'active'));
            let v: TaskFormValue;
            if (this.id) {
                const t = tasks.find((x) => x.id === this.id);
                if (!t) throw new Error('Задача не найдена');
                this.task.set(t);
                v = fromTask(t);
            } else {
                v = emptyTask(options, this.queues().map((q) => q.id));
            }
            this.initial.set(structuredClone(v));
            this.form.set(v);
        } catch (err) {
            this.loadError.set(err);
        } finally {
            this.loading.set(false);
        }
    }

    protected agentLabel(a: string): string {
        return a ? a[0].toUpperCase() + a.slice(1) : a;
    }

    protected patch(p: Partial<TaskFormValue>): void {
        this.form.update((v) => (v ? { ...v, ...p } : v));
    }

    protected async act(action: Action): Promise<void> {
        this.submitted.set(true);
        if (Object.keys(this.errors()).length) {
            setTimeout(() => this.host.nativeElement.querySelector('.kbq-error, kbq-hint[color="error"]')?.scrollIntoView({ block: 'center' }));
            return;
        }
        this.lastAction = action;
        await this.send(action, this.task()?.updated_at);
    }

    /** «Перезаписать»: repeat the last action against the server's current version. */
    protected async overwrite(): Promise<void> {
        const clash = this.submit.clash();
        let at = clash?.kind === 'conflict' ? clash.at : null;
        if (!at) at = (await this.api.tasks().catch(() => [] as AdminTask[])).find((t) => t.id === this.id)?.updated_at ?? null;
        await this.send(this.lastAction, at ?? undefined);
    }

    /** The task was deleted meanwhile: create it again from the form. */
    protected async recreate(): Promise<void> {
        await this.send(this.lastAction, undefined, true);
    }

    private async send(action: Action, updatedAt: string | undefined, forceCreate = false): Promise<void> {
        const v = this.form()!;
        const isPublished = action === 'publish' || action === 'save';
        const res = await this.submit.run(() => {
            if (!this.id || forceCreate) return this.api.createTask(toPayload(v, { is_published: isPublished }));
            return this.api.patchTask(this.id, toPayload(v, { is_published: isPublished, updated_at: updatedAt }));
        });
        if (!res.ok) return;
        if (action === 'publish') {
            const n = v.visibility === 'all' ? null : v.user_ids.length;
            this.notify.success('Задача опубликована', n === null ? 'Её видят все пользователи' : `Её видят ${n} ${users(n)}`);
        } else if (action === 'unpublish') {
            this.notify.info('Задача снята с публикации', 'Пользователи её больше не видят');
        }
        this.leaving = true;
        void this.router.navigateByUrl('/admin/tasks');
    }
}
