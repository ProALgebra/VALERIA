import {
    afterNextRender,
    ChangeDetectionStrategy,
    Component,
    computed,
    effect,
    ElementRef,
    inject,
    Injector,
    input,
    signal,
    untracked,
    viewChild,
} from '@angular/core';
import { FormsModule } from '@angular/forms';
import { Router } from '@angular/router';
import { KbqAlertModule } from '@koobiq/components/alert';
import { KbqBadgeColors, KbqBadgeModule } from '@koobiq/components/badge';
import { KbqButtonModule } from '@koobiq/components/button';
import { KbqButtonToggleModule } from '@koobiq/components/button-toggle';
import { KbqComponentColors, KbqOptionModule, PopUpPlacements } from '@koobiq/components/core';
import { KbqDividerModule } from '@koobiq/components/divider';
import { KbqDropdownModule } from '@koobiq/components/dropdown';
import { KbqFormFieldModule } from '@koobiq/components/form-field';
import { KbqIconModule } from '@koobiq/components/icon';
import { KbqSelectModule } from '@koobiq/components/select';
import { KbqTagsModule } from '@koobiq/components/tags';
import { KbqTextareaModule } from '@koobiq/components/textarea';
import { KbqToolTipModule } from '@koobiq/components/tooltip';
import { ApiService } from '../../core/api/api.service';
import { AuthService } from '../../core/auth/auth.service';
import { ItemsStore } from '../../core/items/items.store';
import { NotifyService } from '../../core/notify/notify.service';
import { failureText, IdempotentAction, isSendShortcut } from '../../shared/chat-composer/send-action';
import { ComposerBoxComponent } from '../../shared/chat-composer/composer-box.component';
import { SendButtonComponent } from '../../shared/chat-composer/send-button.component';
import { clockTime, findActiveDuplicate, TaskDef, TaskRun, TemplateDef } from '../../shared/tasks/task.model';
import { TasksCatalog } from '../../shared/tasks/tasks-catalog.service';
import { HomeDrafts, HomeMode } from './home-drafts';
import { planFirst } from '../../shared/agent-order';

/** `GET /api/options`: agents and models for «Сделать». */
interface ChatOptions {
    agents: string[];
    default_agent: string;
    default_model: string;
    models: { id: string; name: string; provider?: string }[];
}

export const NO_FOLDER_TEXT = 'Администратор ещё не назначил вам рабочую папку';
export const NOTIFY_TEXT = 'Когда задача завершится, Valeria напишет вам в Mattermost';
const AGENT_LABELS: Record<string, string> = { plan: 'Plan', build: 'Build' };

/**
 * Home «Что нужно сделать?» (`/`) and the task preset (`/new/:taskId`), SPEC §8.1.
 *
 * Tasks-only accounts get no mode switch and no templates, and never call `/api/options`, `/api/templates`
 * or `/api/chats*`. The search palette renders this screen underneath itself with `[underlay]="true"`.
 */
@Component({
    selector: 'v-home',
    imports: [
        FormsModule,
        KbqAlertModule,
        KbqBadgeModule,
        KbqButtonModule,
        KbqButtonToggleModule,
        KbqDividerModule,
        KbqDropdownModule,
        KbqFormFieldModule,
        KbqIconModule,
        KbqOptionModule,
        KbqSelectModule,
        KbqTagsModule,
        KbqTextareaModule,
        KbqToolTipModule,
        ComposerBoxComponent,
        SendButtonComponent,
    ],
    changeDetection: ChangeDetectionStrategy.OnPush,
    host: { class: 'v-home', '[attr.inert]': 'underlay() ? "" : null', '[attr.aria-hidden]': 'underlay() || null' },
    templateUrl: './home.component.html',
    styleUrl: './home.component.scss',
})
export class HomeComponent {
    /** Route param of `/new/:taskId`. */
    readonly taskId = input<string | undefined>(undefined);
    /** Rendered under the search palette: inert, no autofocus. */
    readonly underlay = input(false);

    private readonly api = inject(ApiService);
    private readonly auth = inject(AuthService);
    private readonly router = inject(Router);
    private readonly notify = inject(NotifyService);
    private readonly items = inject(ItemsStore);
    private readonly drafts = inject(HomeDrafts);
    private readonly catalog = inject(TasksCatalog);
    private readonly injector = inject(Injector);

    protected readonly colors = KbqComponentColors;
    protected readonly badgeColors = KbqBadgeColors;
    protected readonly placements = PopUpPlacements;
    protected readonly noFolderText = NO_FOLDER_TEXT;
    protected readonly notifyText = NOTIFY_TEXT;
    protected readonly agentLabels = AGENT_LABELS;

    private readonly field = viewChild<ElementRef<HTMLTextAreaElement>>('field');

    protected readonly user = this.auth.user;
    protected readonly tasksOnly = this.auth.tasksOnly;
    protected readonly noFolder = computed(() => !!this.user() && !this.user()?.workspace_path);
    protected readonly login = computed(() => this.user()?.mattermost_username || this.user()?.login || '');

    protected readonly tasks = this.catalog.tasks;
    protected readonly topTasks = computed(() => this.tasks().slice(0, 4));
    protected readonly templates = signal<TemplateDef[]>([]);
    protected readonly variantsCount = computed(() => this.tasks().length + this.templates().length);
    protected readonly options = signal<ChatOptions | null>(null);
    /** Mode toggle order follows the prototype: «План | Сборка». */
    protected readonly agentOrder = computed(() => planFirst(this.options()?.agents ?? []));

    /** Task of `/new/:taskId`, once the catalog is loaded. */
    protected readonly preset = computed<TaskDef | null>(() => {
        const id = this.taskId();
        return id === undefined ? null : (this.catalog.find(id) ?? null);
    });

    private readonly storedMode = signal<HomeMode>(this.drafts.mode());
    protected readonly mode = computed<HomeMode>(() => (this.tasksOnly() ? 'ask' : this.storedMode()));
    protected readonly text = signal(this.drafts.text());
    protected readonly agent = signal('build');
    protected readonly model = signal('');

    protected readonly addition = signal('');
    protected readonly duplicate = signal<TaskRun | null>(null);
    protected readonly duplicateTime = computed(() => {
        const run = this.duplicate();
        return run ? clockTime(run.started_at ?? run.created_at) : '';
    });

    protected readonly sending = signal(false);
    protected readonly launching = signal(false);

    protected readonly sendLabel = computed(() => (this.mode() === 'do' ? 'Начать' : 'Спросить'));
    protected readonly sendReason = computed(() => {
        if (this.noFolder()) return NO_FOLDER_TEXT;
        if (!this.text().trim()) return this.mode() === 'do' ? 'Опишите, что нужно сделать' : 'Напишите вопрос';
        return '';
    });
    protected readonly launchReason = computed(() => (this.noFolder() ? NO_FOLDER_TEXT : ''));
    protected readonly placeholder = computed(() =>
        this.mode() === 'do'
            ? 'Например: переведи пакет net-utils на Conan 2, сначала покажи план'
            : 'Например: почему nightly-сборка падает только на gcc 13?',
    );

    private readonly sendKey = new IdempotentAction();
    private readonly launchKey = new IdempotentAction();

    constructor() {
        void this.catalog.ensure().catch(() => undefined);

        // `/new/:taskId` with an unknown task (removed or hidden): back to home.
        effect(() => {
            const id = this.taskId();
            if (id === undefined || this.underlay() || !this.catalog.loaded() || this.catalog.find(id)) return;
            untracked(() => void this.router.navigate(['/'], { replaceUrl: true }));
        });

        // Full accounts only: tasks-only never calls /api/templates and /api/options (SPEC §4).
        effect(() => {
            const user = this.user();
            if (!user || this.tasksOnly()) return;
            untracked(() => this.loadFullOptions());
        });

        // Preset: restore the task addition and focus it.
        effect(() => {
            const id = this.taskId();
            untracked(() => {
                this.duplicate.set(null);
                this.addition.set(id === undefined ? '' : this.drafts.addition(id));
            });
            this.focusField();
        });
    }

    protected setMode(mode: HomeMode): void {
        if (mode === this.mode()) return;
        this.storedMode.set(mode);
        this.drafts.setMode(mode);
        this.focusField();
    }

    protected setText(value: string): void {
        this.text.set(value);
        this.drafts.setText(value);
    }

    protected setAddition(value: string): void {
        this.addition.set(value);
        const id = this.taskId();
        if (id !== undefined) this.drafts.setAddition(id, value);
        this.duplicate.set(null);
    }

    protected openTask(task: TaskDef): void {
        void this.router.navigate(['/new', task.id]);
    }

    protected useTemplate(template: TemplateDef): void {
        this.storedMode.set('do');
        this.drafts.prepare('do', template.content);
        this.text.set(template.content);
        this.focusField();
    }

    protected templateExcerpt(template: TemplateDef): string {
        return `${template.content.slice(0, 70)}…`;
    }

    protected clearPreset(): void {
        void this.router.navigate(['/']);
    }

    protected isPlanFirst(task: TaskDef): boolean {
        return !!task.plan_first;
    }

    protected isDefaultModel(id: string): boolean {
        return id === this.options()?.default_model;
    }

    protected onKeydown(event: KeyboardEvent, action: 'send' | 'launch'): void {
        if (!isSendShortcut(event)) return;
        event.preventDefault();
        if (action === 'send') void this.send();
        else void this.launch();
    }

    protected openDuplicate(): void {
        const run = this.duplicate();
        if (run) void this.router.navigate(['/tasks/runs', run.id]);
    }

    /** «Спросить» → `POST /api/consultations`; «Начать» → `POST /api/chats`. */
    protected async send(): Promise<void> {
        if (this.sending() || this.sendReason()) return;
        const message = this.text().trim();
        const mode = this.mode();
        this.sending.set(true);
        try {
            if (mode === 'ask') {
                const key = this.sendKey.keyFor(['ask', message]);
                const res = await this.api.post<{ id: string | number }>(
                    '/api/consultations',
                    { message },
                    { idempotencyKey: key },
                );
                this.sendKey.done();
                this.notify.info('Вопрос отправлен');
                this.afterSend();
                void this.router.navigate(['/questions', res.id]);
            } else {
                const body = { message, agent: this.agent(), model: this.model() || undefined };
                const key = this.sendKey.keyFor(['do', body]);
                const res = await this.api.post<{ chat: { id: string | number } }>('/api/chats', body, {
                    idempotencyKey: key,
                });
                this.sendKey.done();
                this.afterSend();
                void this.router.navigate(['/folder', res.chat.id]);
            }
        } catch (error) {
            this.notify.error(
                failureText(mode === 'ask' ? 'Не удалось отправить вопрос' : 'Не удалось начать работу', error),
                undefined,
                error,
            );
        } finally {
            this.sending.set(false);
        }
    }

    /** «Запустить»: client duplicate check, then `POST /api/tasks/:id/runs`. */
    protected async launch(): Promise<void> {
        const task = this.preset();
        if (!task || this.launching() || this.launchReason()) return;
        const userInput = this.addition().trim();
        this.launching.set(true);
        this.duplicate.set(null);
        try {
            const runs = await this.api.get<TaskRun[]>('/api/task-runs').catch(() => [] as TaskRun[]);
            const duplicate = findActiveDuplicate(Array.isArray(runs) ? runs : [], task.id, userInput);
            if (duplicate) {
                this.duplicate.set(duplicate);
                return;
            }
            const key = this.launchKey.keyFor([task.id, userInput]);
            const run = await this.api.post<TaskRun>(
                `/api/tasks/${encodeURIComponent(String(task.id))}/runs`,
                { user_input: userInput },
                { idempotencyKey: key },
            );
            this.launchKey.done();
            this.notify.info('Задача поставлена в очередь', `${NOTIFY_TEXT} @${this.login()}`);
            this.drafts.setAddition(task.id, '');
            void this.items.refresh().catch(() => undefined);
            void this.router.navigate(['/tasks/runs', run.id]);
        } catch (error) {
            this.notify.error(failureText('Не удалось запустить задачу', error), undefined, error);
        } finally {
            this.launching.set(false);
        }
    }

    private afterSend(): void {
        this.text.set('');
        this.drafts.setText('');
        void this.items.refresh().catch(() => undefined);
    }

    private loadFullOptions(): void {
        this.api.get<TemplateDef[]>('/api/templates').then(
            (list) => this.templates.set(Array.isArray(list) ? list : []),
            () => this.templates.set([]),
        );
        this.api.get<ChatOptions>('/api/options').then(
            (opts) => {
                this.options.set(opts);
                this.agent.set(opts.default_agent || opts.agents?.[0] || 'build');
                this.model.set(opts.default_model || opts.models?.[0]?.id || '');
            },
            () => this.options.set(null),
        );
    }

    private focusField(): void {
        if (untracked(() => this.underlay())) return;
        afterNextRender(() => this.field()?.nativeElement.focus({ preventScroll: true }), {
            injector: this.injector,
        });
    }
}
