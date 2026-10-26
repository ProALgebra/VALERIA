import {
    afterNextRender,
    ChangeDetectionStrategy,
    Component,
    computed,
    DestroyRef,
    effect,
    ElementRef,
    inject,
    Injector,
    input,
    signal,
    untracked,
    viewChild,
} from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { KbqAlertModule } from '@koobiq/components/alert';
import { KbqButtonModule } from '@koobiq/components/button';
import { KbqButtonToggleModule } from '@koobiq/components/button-toggle';
import { KbqCodeBlockModule } from '@koobiq/components/code-block';
import { KbqComponentColors, PopUpPlacements } from '@koobiq/components/core';
import { KbqDlModule } from '@koobiq/components/dl';
import { KbqFormFieldModule } from '@koobiq/components/form-field';
import { KbqIconModule } from '@koobiq/components/icon';
import { KbqSelectModule } from '@koobiq/components/select';
import { KbqToolTipModule } from '@koobiq/components/tooltip';
import { ApiError } from '../../core/api/api-error';
import { ApiService } from '../../core/api/api.service';
import { EventsService, ServerEvent } from '../../core/events/events.service';
import { ItemsStore } from '../../core/items/items.store';
import { NotifyService } from '../../core/notify/notify.service';
import { describeError, errorCodeText } from '../../core/status/error-codes';
import { isWorking, KIND_ICON, statusLabel } from '../../core/status/status';
import { ScreenHeaderComponent } from '../../shell/screen-header/screen-header.component';
import { ChatAsideComponent } from '../../shell/aside/chat-aside.component';
import { AgentStepsComponent } from '../../shared/agent-steps/agent-steps.component';
import { messageModel, messageText, OcMessage, OcMessageInfo, OcPart } from '../../shared/agent-steps/opencode.model';
import { MessageBlock, partsToBlocks } from '../../shared/agent-steps/steps';
import { ChatComposerComponent } from '../../shared/chat-composer/chat-composer.component';
import { IdempotentAction } from '../../shared/chat-composer/send-action';
import { SendButtonComponent } from '../../shared/chat-composer/send-button.component';
import {
    BotMessageComponent,
    messageTime,
    StatusLineComponent,
    UserMessageComponent,
} from '../../shared/chat-message/chat-message.component';
import { ClarifyCardComponent } from '../../shared/clarify-card/clarify-card.component';
import { QuestionRequest } from '../../shared/clarify-card/clarify.model';
import { MarkdownViewComponent } from '../../shared/markdown-view/markdown-view.component';
import { NotFoundComponent } from '../../shared/not-found/not-found.component';
import { PageStateComponent } from '../../shared/page-state/page-state.component';
import { QueueJob } from '../question/question.model';
import {
    agentLabel,
    agentOf,
    alwaysLabel,
    FolderChat,
    FolderOptions,
    folderPhase,
    folderStateText,
    isFolderBusy,
    lastAssistant,
    McpServer,
    mcpConnected,
    PermissionRequest,
    PermissionResponse,
    upsertMessageInfo,
    upsertPart,
    workingText,
} from './folder.model';
import { planFirst } from '../../shared/agent-order';

interface ViewMessage {
    id: string;
    role: 'user' | 'assistant';
    text: string;
    time: string;
    modeline: string;
    blocks: MessageBlock[];
}

const BUSY_REASON = 'Агент ещё работает, дождитесь ответа';

/** Folder work screen (SPEC §8.4): an OpenCode session in the user's workspace. */
@Component({
    selector: 'v-folder',
    imports: [
        ScreenHeaderComponent,
        ChatAsideComponent,
        PageStateComponent,
        NotFoundComponent,
        KbqAlertModule,
        KbqButtonModule,
        KbqButtonToggleModule,
        KbqCodeBlockModule,
        KbqDlModule,
        KbqFormFieldModule,
        KbqIconModule,
        KbqSelectModule,
        KbqToolTipModule,
        AgentStepsComponent,
        UserMessageComponent,
        BotMessageComponent,
        StatusLineComponent,
        MarkdownViewComponent,
        ClarifyCardComponent,
        ChatComposerComponent,
        SendButtonComponent,
    ],
    changeDetection: ChangeDetectionStrategy.OnPush,
    template: `
        <v-screen-header [title]="title()" [chat]="item()" [asideToggle]="!notFound()">
            @if (!notFound()) {
                <span
                    vHeaderLead
                    class="v-titleic"
                    tabindex="0"
                    [kbqTooltip]="rwTip()"
                    [kbqPlacement]="placements.Bottom"
                    [attr.aria-label]="rwTip()"
                >
                    <i [kbq-icon]="folderIcon" [color]="colors.Theme"></i>
                </span>
            }
        </v-screen-header>

        @if (notFound()) {
            <v-not-found />
        } @else {
            <div class="v-row">
                <div class="v-main">
                    <v-page-state [loading]="loading()" [error]="error()" (retry)="load()">
                        <div class="v-content" #scroller>
                            <div class="v-chat">
                                @if (disconnected()) {
                                    <kbq-alert class="v-lost" alertStyle="colored" alertColor="warning" [compact]="true">
                                        <i kbq-icon="kbq-triangle-exclamation_16"></i>
                                        Связь с агентом потеряна, переподключаемся
                                    </kbq-alert>
                                }
                                @for (m of view(); track m.id) {
                                    @if (m.role === 'user') {
                                        <v-user-message [text]="m.text" [time]="m.time" />
                                        @if (m.modeline) {
                                            <div class="kbq-text-compact v-modeline">{{ m.modeline }}</div>
                                        }
                                    } @else {
                                        <v-bot-message [time]="m.time" [copyText]="m.id === copyId() ? m.text : ''">
                                            @for (b of m.blocks; track $index) {
                                                @if (b.kind === 'steps') {
                                                    <v-agent-steps [steps]="b.steps" />
                                                } @else {
                                                    <v-markdown-view [text]="b.text" />
                                                }
                                            }
                                            @if (m.id === lastBotId()) {
                                                @switch (phase()) {
                                                    @case ('working') {
                                                        @if (!pendingText()) {
                                                            <v-status-line [text]="workingLine()" />
                                                        }
                                                    }
                                                    @case ('permission') {
                                                        <div class="v-waiting">
                                                            <i kbq-icon="kbq-lock_16" [color]="colors.Warning" aria-hidden="true"></i>
                                                            Ждёт вашего разрешения
                                                        </div>
                                                    }
                                                    @case ('plan') {
                                                        <div class="v-planact">
                                                            <v-send-button
                                                                label="Выполнить план в режиме Build"
                                                                icon="kbq-play_16"
                                                                [shortcut]="false"
                                                                [sending]="building()"
                                                                (pressed)="runPlan()"
                                                            >
                                                                Выполнить план в режиме Build
                                                            </v-send-button>
                                                            <span class="v-secondary">или напишите, что поправить</span>
                                                        </div>
                                                    }
                                                }
                                            }
                                        </v-bot-message>
                                    }
                                }
                                @if (pendingText(); as p) {
                                    <v-user-message [text]="p" />
                                }
                                @if (phase() === 'working' && (pendingText() || !lastIsBot())) {
                                    <v-status-line [text]="workingLine()" />
                                }
                                @if (failure(); as f) {
                                    <kbq-alert class="v-failed" alertStyle="colored" alertColor="error" [compact]="true">
                                        <i kbq-icon="kbq-circle-exclamation_16"></i>
                                        {{ f.text }}
                                        @if (f.code) {
                                            <span class="v-tertiary">{{ f.code }}</span>
                                        }
                                    </kbq-alert>
                                }
                            </div>
                        </div>
                        @if (chat()) {
                            <div class="v-dock">
                                <div class="v-chat">
                                    @if (phase() === 'permission' && permission(); as p) {
                                        <kbq-alert
                                            alertStyle="colored"
                                            alertColor="warning"
                                            [compact]="true"
                                            role="group"
                                            aria-label="Valeria просит разрешение на команду"
                                        >
                                            <i kbq-icon="kbq-lock_16"></i>
                                            <div kbq-alert-title>Valeria просит разрешение на команду</div>
                                            @if (p.metadata?.command; as cmd) {
                                                <kbq-code-block
                                                    class="v-perm__code"
                                                    [files]="[{ content: cmd, language: 'bash' }]"
                                                    [softWrap]="true"
                                                />
                                            }
                                            @if (p.metadata?.description; as why) {
                                                <div class="v-perm__why"><span class="v-secondary">Зачем:</span> {{ why }}</div>
                                            }
                                            <button
                                                kbq-alert-control
                                                kbq-button
                                                type="button"
                                                [color]="colors.Contrast"
                                                [disabled]="!!responding()"
                                                [class.kbq-progress]="responding() === 'once'"
                                                (click)="respond(p, 'once')"
                                            >
                                                Разрешить один раз
                                            </button>
                                            @if (alwaysText(); as always) {
                                                <button
                                                    kbq-alert-control
                                                    kbq-button
                                                    type="button"
                                                    [color]="colors.ContrastFade"
                                                    [disabled]="!!responding()"
                                                    [class.kbq-progress]="responding() === 'always'"
                                                    (click)="respond(p, 'always')"
                                                >
                                                    Всегда разрешать&nbsp;<b>{{ always }}</b>
                                                </button>
                                            }
                                            <button
                                                kbq-alert-control
                                                kbq-button
                                                type="button"
                                                [color]="colors.ContrastFade"
                                                [disabled]="!!responding()"
                                                [class.kbq-progress]="responding() === 'reject'"
                                                (click)="respond(p, 'reject')"
                                            >
                                                Отклонить
                                            </button>
                                        </kbq-alert>
                                    } @else if (phase() === 'clarify' && pendingQuestion(); as q) {
                                        <v-clarify-card [request]="q" [submit]="reply" />
                                    } @else if (phase() !== 'permission' && phase() !== 'clarify') {
                                        <v-chat-composer
                                            [draftKey]="'folder:' + id()"
                                            placeholder="Сообщение агенту"
                                            label="Отправить"
                                            [busy]="busy()"
                                            [busyReason]="busyReason"
                                            emptyReason="Напишите сообщение"
                                            [submit]="send"
                                            [stoppable]="working()"
                                            [stopping]="aborting() || status() === 'cancelling'"
                                            (stop)="abort()"
                                        >
                                            <div class="v-bar">
                                                <kbq-button-toggle-group
                                                    [value]="agent()"
                                                    [disabled]="busy()"
                                                    aria-label="Режим агента"
                                                    (change)="agent.set($event.value)"
                                                >
                                                    @for (a of agents(); track a) {
                                                        <kbq-button-toggle [value]="a">{{ agentName(a) }}</kbq-button-toggle>
                                                    }
                                                </kbq-button-toggle-group>
                                                @if (options(); as o) {
                                                    <kbq-form-field class="v-select">
                                                        <kbq-select
                                                            [value]="model()"
                                                            [disabled]="busy()"
                                                            aria-label="Модель"
                                                            (selectionChange)="model.set($event.value)"
                                                        >
                                                            <kbq-select-trigger>{{ model() }}</kbq-select-trigger>
                                                            <kbq-optgroup label="Модель">
                                                                @for (m of o.models; track m.id) {
                                                                    <kbq-option [value]="m.id">
                                                                        {{ m.id }}
                                                                        @if (m.id === o.default_model) {
                                                                            <span class="kbq-text-compact v-hint">по умолчанию</span>
                                                                        }
                                                                    </kbq-option>
                                                                }
                                                            </kbq-optgroup>
                                                        </kbq-select>
                                                    </kbq-form-field>
                                                }
                                            </div>
                                        </v-chat-composer>
                                    }
                                </div>
                            </div>
                        }
                    </v-page-state>
                </div>
                @if (chat(); as c) {
                    <v-chat-aside>
                        <kbq-dl [vertical]="true">
                            <kbq-dt>Рабочая папка</kbq-dt>
                            <kbq-dd class="v-mono">{{ c.workspace_snapshot || '—' }}</kbq-dd>
                            <kbq-dt>Состояние</kbq-dt>
                            <kbq-dd>{{ stateText() }}</kbq-dd>
                            <kbq-dt>Подключения</kbq-dt>
                            <kbq-dd class="v-mcp">
                                @for (s of mcp(); track s.name) {
                                    @if (connected(s)) {
                                        <span><i kbq-icon="kbq-circle-check_16" [color]="colors.Success" aria-hidden="true"></i>{{ s.name }}</span>
                                    } @else {
                                        <span
                                            ><i kbq-icon="kbq-circle-exclamation_16" [color]="colors.Warning" aria-hidden="true"></i>{{ s.name }} ·
                                            переподключается</span
                                        >
                                    }
                                } @empty {
                                    <span>—</span>
                                }
                            </kbq-dd>
                        </kbq-dl>
                    </v-chat-aside>
                }
            </div>
        }
    `,
    styles: [
        `
        :host { display: flex; flex-direction: column; min-height: 0; }
        .v-row { flex: 1 1 auto; display: flex; min-height: 0; }
        .v-main { flex: 1 1 auto; min-width: 0; display: flex; flex-direction: column; min-height: 0; }
        .v-main > v-page-state { flex: 1 1 auto; display: flex; flex-direction: column; min-height: 0; }
        .v-content { flex: 1 1 auto; min-height: 0; overflow: auto; padding: var(--kbq-size-l) var(--kbq-size-xxl) var(--kbq-size-xxl); }
        .v-chat { max-width: 800px; margin: 0 auto; }
        .v-dock {
            flex: none;
            padding: var(--kbq-size-m) var(--kbq-size-xxl) var(--kbq-size-l);
            border-top: var(--kbq-size-border-width) solid var(--kbq-line-contrast-less);
            background: var(--kbq-background-bg);
        }
        .v-secondary { color: var(--kbq-foreground-contrast-secondary); }
        .v-tertiary { color: var(--kbq-foreground-contrast-tertiary); }
        .v-titleic { display: inline-flex; flex: none; align-items: center; margin-right: var(--kbq-size-s); border-radius: var(--kbq-size-xxs); }
        .v-titleic:focus-visible { outline: var(--kbq-size-3xs) solid var(--kbq-states-line-focus-theme); outline-offset: var(--kbq-size-3xs); }
        .v-modeline {
            margin: var(--kbq-size-m) 0 calc(-1 * var(--kbq-size-xxs));
            color: var(--kbq-foreground-contrast-tertiary);
            text-align: right;
        }
        .v-waiting {
            display: flex;
            align-items: center;
            gap: var(--kbq-size-s);
            margin-top: var(--kbq-size-s);
            color: var(--kbq-foreground-warning);
            font-weight: var(--kbq-typography-text-normal-medium-font-weight);
        }
        .v-planact { display: flex; align-items: center; gap: var(--kbq-size-m); margin-top: var(--kbq-size-m); }
        .v-lost, .v-failed { margin: var(--kbq-size-s) 0; }
        .v-perm__code { margin-top: var(--kbq-size-s); }
        .v-perm__why { margin-top: var(--kbq-size-s); }
        .v-bar { display: flex; align-items: center; flex-wrap: wrap; gap: var(--kbq-size-s); }
        .v-select { width: auto; min-width: 0; max-width: 260px; }
        .v-hint { margin-left: auto; padding-left: var(--kbq-size-l); color: var(--kbq-foreground-contrast-tertiary); }
        kbq-dd.v-mono { font-family: var(--kbq-typography-mono-normal-font-family, monospace); word-break: break-all; }
        .v-mcp { display: flex; flex-direction: column; gap: var(--kbq-size-xxs); }
        .v-mcp span { display: inline-flex; align-items: center; gap: var(--kbq-size-xs); }
    `,
    ],
})
export class FolderComponent {
    /** Route param (`withComponentInputBinding`). */
    readonly id = input.required<string>();

    private readonly api = inject(ApiService);
    private readonly events = inject(EventsService);
    private readonly items = inject(ItemsStore);
    private readonly notify = inject(NotifyService);
    private readonly injector = inject(Injector);
    private readonly scroller = viewChild<ElementRef<HTMLElement>>('scroller');

    protected readonly colors = KbqComponentColors;
    protected readonly placements = PopUpPlacements;
    protected readonly folderIcon = KIND_ICON.folder;
    protected readonly busyReason = BUSY_REASON;

    protected readonly chat = signal<FolderChat | null>(null);
    protected readonly messages = signal<OcMessage[]>([]);
    protected readonly loading = signal(true);
    protected readonly error = signal<unknown>(null);
    protected readonly notFound = signal(false);
    protected readonly questions = signal<QuestionRequest[]>([]);
    protected readonly permissions = signal<PermissionRequest[]>([]);
    protected readonly mcp = signal<McpServer[]>([]);
    protected readonly options = signal<FolderOptions | null>(null);
    protected readonly job = signal<QueueJob | null>(null);
    protected readonly disconnected = signal(false);
    protected readonly pendingText = signal('');
    protected readonly agent = signal('build');
    protected readonly model = signal('');
    protected readonly building = signal(false);
    protected readonly aborting = signal(false);
    protected readonly responding = signal<PermissionResponse | null>(null);

    protected readonly item = computed(() => this.items.byChatId(this.id()));
    protected readonly status = computed(() => this.chat()?.status ?? '');
    protected readonly title = computed(() => (this.notFound() ? '' : (this.chat()?.title ?? '')));
    protected readonly phase = computed(() => folderPhase(this.status(), this.messages()));
    protected readonly working = computed(() => isWorking(this.status()));
    protected readonly busy = computed(() => isFolderBusy(this.status()) || !!this.pendingText());
    protected readonly workingLine = computed(() =>
        this.pendingText() ? (this.agent() === 'plan' ? 'Составляет план' : 'Работает') : workingText(this.messages()),
    );
    protected readonly lastBotId = computed(() => lastAssistant(this.messages())?.info.id ?? '');
    protected readonly lastIsBot = computed(() => this.messages().at(-1)?.info.role === 'assistant');
    protected readonly copyId = computed(() => (this.phase() === 'done' ? this.lastBotId() : ''));
    protected readonly permission = computed(() => this.permissions()[0] ?? null);
    protected readonly alwaysText = computed(() => {
        const p = this.permission();
        return p ? alwaysLabel(p) : '';
    });
    protected readonly pendingQuestion = computed(() => this.questions()[0] ?? null);
    protected readonly agents = computed(() => planFirst(this.options()?.agents ?? ['plan', 'build']));
    protected readonly stateText = computed(() =>
        folderStateText(this.status(), this.messages(), statusLabel(this.status(), 'folder')),
    );
    protected readonly rwTip = computed(
        () => `Может менять файлы. Агент правит файлы и запускает команды в папке ${this.chat()?.workspace_snapshot ?? ''}.`,
    );
    /** Last run failed: the chat is idle again, the queue row keeps the error code. */
    protected readonly failure = computed(() => {
        const job = this.job();
        if (!job || job.status !== 'failed' || isFolderBusy(this.status())) return null;
        return errorCodeText(job.error_code);
    });

    protected readonly view = computed<ViewMessage[]>(() =>
        this.messages().map((m) => {
            const text = messageText(m);
            const user = m.info.role === 'user';
            const model = messageModel(m.info);
            const agent = agentLabel(agentOf(m.info));
            return {
                id: m.info.id,
                role: m.info.role,
                text,
                time: messageTime(user ? m.info.time?.created : (m.info.time?.completed ?? m.info.time?.created)),
                modeline: user && agent ? [agent, model].filter(Boolean).join(' · ') : '',
                blocks: user ? [] : partsToBlocks(m.parts),
            };
        }),
    );

    private readonly sendAction = new IdempotentAction();
    private readonly buildAction = new IdempotentAction();
    private readonly replyAction = new IdempotentAction();
    private loadSeq = 0;

    constructor() {
        effect(() => {
            const id = this.id();
            untracked(() => {
                this.chat.set(null);
                this.messages.set([]);
                this.questions.set([]);
                this.permissions.set([]);
                this.job.set(null);
                this.notFound.set(false);
                this.disconnected.set(false);
                this.pendingText.set('');
                this.options.set(null);
                void this.load();
            });
        });

        this.events
            .on('opencode.event')
            .pipe(takeUntilDestroyed())
            .subscribe((e) => this.onOpencode(e));
        this.events
            .on('opencode.disconnected')
            .pipe(takeUntilDestroyed())
            .subscribe((e) => {
                const ws = this.chat()?.workspace_snapshot;
                if (!e['workspace'] || e['workspace'] === ws) this.disconnected.set(true);
            });
        this.events
            .on('chat.questions.updated')
            .pipe(takeUntilDestroyed())
            .subscribe((e) => {
                if (this.isMine(e)) void this.loadQuestions();
            });
        this.events
            .on('job.updated')
            .pipe(takeUntilDestroyed())
            .subscribe((e) => {
                const job = (e['job'] ?? {}) as Partial<QueueJob> & { status?: string };
                if (String(job.chat_id) !== this.id() || !job.status) return;
                const unfinished = ['queued', 'dispatching', 'running', 'waiting_permission', 'waiting_question', 'cancelling'];
                const status = unfinished.includes(job.status) ? job.status : 'idle';
                this.setStatus(status);
                if (job.status === 'failed') this.job.set(job as QueueJob);
                else if (status !== 'idle') this.job.set(null);
                if (status === 'waiting_permission') void this.loadPermissions();
                if (status === 'waiting_question') void this.loadQuestions();
                if (status === 'idle') void this.load(true);
            });
        this.events
            .on('chat.deleted')
            .pipe(takeUntilDestroyed())
            .subscribe((e) => {
                if (String(e.chat_id) === this.id()) this.markGone();
            });
        this.events
            .on('chat.updated')
            .pipe(takeUntilDestroyed())
            .subscribe((e) => {
                if (String(e.chat_id) !== this.id()) return;
                this.chat.update((c) => (c ? { ...c, status: e.status ?? c.status, title: e.title ?? c.title } : c));
            });
        this.events.reconnected$.pipe(takeUntilDestroyed()).subscribe(() => void this.load(true));
        inject(DestroyRef).onDestroy(this.events.pollWhileDisconnected(() => void this.load(true), 5000));
    }

    /** Loads the chat with its messages, cards, connections and options. `quiet` keeps the content. */
    async load(quiet = false): Promise<void> {
        const id = this.id();
        const base = `/api/chats/${encodeURIComponent(id)}`;
        const seq = ++this.loadSeq;
        if (!quiet) {
            this.loading.set(true);
            this.error.set(null);
        }
        try {
            const [chat, messages] = await Promise.all([
                this.api.get<FolderChat>(base),
                this.api.get<OcMessage[]>(`${base}/messages`),
            ]);
            if (seq !== this.loadSeq) return;
            const grew = (messages?.length ?? 0) !== this.messages().length;
            this.chat.set(chat);
            this.messages.set(Array.isArray(messages) ? messages : []);
            if (this.userCount(this.messages()) > this.userCountAtSend) this.pendingText.set('');
            this.disconnected.set(false);
            this.items.patchChat(id, { status: chat.status, title: chat.title || undefined });
            await Promise.all([
                this.loadQuestions(),
                this.loadPermissions(),
                this.loadMcp(),
                this.options() ? null : this.loadOptions(),
                isFolderBusy(chat.status) ? (this.job.set(null), null) : this.loadJob(),
            ]);
            if (grew) this.scrollToEnd();
        } catch (err) {
            if (seq !== this.loadSeq) return;
            if (err instanceof ApiError && err.isNotFound) this.notFound.set(true);
            else if (!quiet) this.error.set(err);
        } finally {
            if (seq === this.loadSeq) this.loading.set(false);
        }
    }

    private get base(): string {
        return `/api/chats/${encodeURIComponent(this.id())}`;
    }

    /** The chat was deleted (here or in another tab): show «not found» and drop loads in flight. */
    private markGone(): void {
        this.loadSeq++;
        this.loading.set(false);
        this.error.set(null);
        this.notFound.set(true);
    }

    /** Follow-up loads drop their result when the user has switched to another chat meanwhile. */
    private async loadQuestions(): Promise<void> {
        const id = this.id();
        try {
            const list = await this.api.get<QuestionRequest[]>(`${this.base}/questions`);
            if (id !== this.id()) return;
            this.questions.set(Array.isArray(list) ? list.filter((q) => q.questions?.length) : []);
        } catch (err) {
            if (id !== this.id()) return;
            this.notify.error('Не удалось загрузить вопрос агента', describeError(err).text, err);
        }
    }

    private async loadPermissions(): Promise<void> {
        const id = this.id();
        try {
            const list = await this.api.get<PermissionRequest[]>(`${this.base}/permissions`);
            if (id !== this.id()) return;
            this.permissions.set(Array.isArray(list) ? list : []);
        } catch (err) {
            if (id !== this.id()) return;
            this.notify.error('Не удалось загрузить запрос разрешения', describeError(err).text, err);
        }
    }

    private async loadMcp(): Promise<void> {
        const id = this.id();
        try {
            const res = await this.api.get<{ servers?: McpServer[] }>(`${this.base}/mcp`);
            if (id === this.id()) this.mcp.set(res?.servers ?? []);
        } catch {
            if (id === this.id()) this.mcp.set([]);
        }
    }

    private async loadOptions(): Promise<void> {
        const id = this.id();
        try {
            const o = await this.api.get<FolderOptions>(`/api/options?chat_id=${encodeURIComponent(id)}`);
            if (id !== this.id()) return;
            this.options.set(o);
            const agents = o.agents ?? [];
            const agent = o.selected_agent ?? o.default_agent ?? 'build';
            this.agent.set(agents.includes(agent) ? agent : (agents[0] ?? 'build'));
            this.model.set(o.selected_model ?? o.default_model ?? '');
        } catch (err) {
            if (id !== this.id()) return;
            this.notify.error('Не удалось загрузить список моделей', describeError(err).text, err);
        }
    }

    private async loadJob(): Promise<void> {
        const id = this.id();
        try {
            const jobs = await this.api.get<QueueJob[]>('/api/queue/me');
            if (id !== this.id()) return;
            const mine = (jobs ?? [])
                .filter((j) => String(j.chat_id) === id)
                .sort((a, b) => String(b.created_at ?? '').localeCompare(String(a.created_at ?? '')));
            this.job.set(mine[0] ?? null);
        } catch {
            if (id === this.id()) this.job.set(null);
        }
    }

    private isMine(e: ServerEvent): boolean {
        const sid = this.chat()?.opencode_session_id;
        return !!sid && e['session_id'] === sid;
    }

    private onOpencode(e: ServerEvent): void {
        if (!this.isMine(e)) return;
        this.disconnected.set(false);
        const payload =
            (e['event'] as { payload?: { type?: string; properties?: Record<string, unknown> } })?.payload ?? {};
        const props = payload.properties ?? {};
        switch (payload.type) {
            case 'message.updated': {
                const info = props['info'] as OcMessageInfo | undefined;
                if (!info?.id) return;
                const before = this.messages().length;
                this.messages.update((list) => upsertMessageInfo(list, info));
                if (info.role === 'user' && this.userCount(this.messages()) > this.userCountAtSend) this.pendingText.set('');
                if (this.messages().length !== before) this.scrollToEnd();
                return;
            }
            case 'message.part.updated': {
                const part = props['part'] as OcPart | undefined;
                if (part?.id) this.messages.update((list) => upsertPart(list, part));
                return;
            }
            case 'permission.asked':
            case 'permission.replied':
                void this.loadPermissions();
                return;
            case 'session.idle':
                void this.load(true);
                return;
        }
    }

    /** `id` is the chat a request was made for: after an awaited request the screen may show another chat. */
    private setStatus(status: string, id = this.id()): void {
        this.items.patchChat(id, { status });
        if (id === this.id()) this.chat.update((c) => (c ? { ...c, status } : c));
    }

    private userCountAtSend = Number.POSITIVE_INFINITY;
    private userCount(list: OcMessage[]): number {
        return list.filter((m) => m.info.role === 'user').length;
    }

    protected agentName(agent: string): string {
        return agentLabel(agent);
    }

    protected connected(server: McpServer): boolean {
        return mcpConnected(server);
    }

    protected readonly send = (text: string): Promise<boolean> =>
        this.postMessage({ message: text, agent: this.agent(), model: this.model() || undefined }, this.sendAction);

    /** «Выполнить план в режиме Build». */
    protected async runPlan(): Promise<void> {
        if (this.building()) return;
        this.building.set(true);
        const id = this.id();
        try {
            if (await this.postMessage({ message: 'Выполни план', agent: 'build', model: this.model() || undefined }, this.buildAction)) {
                if (id === this.id()) this.agent.set('build');
            }
        } finally {
            this.building.set(false);
        }
    }

    private async postMessage(body: { message: string; agent: string; model?: string }, action: IdempotentAction): Promise<boolean> {
        const id = this.id();
        try {
            // The chat id is part of the key: the same text in another chat is another action.
            await this.api.post(`${this.base}/messages`, body, { idempotencyKey: action.keyFor({ chat: id, ...body }) });
            action.done();
            if (id !== this.id()) {
                this.items.patchChat(id, { status: 'queued' });
                return true;
            }
            this.userCountAtSend = this.userCount(this.messages());
            this.pendingText.set(body.message);
            this.job.set(null);
            this.setStatus('queued');
            this.scrollToEnd();
            return true;
        } catch (err) {
            if (err instanceof ApiError && err.isConflict) {
                this.notify.error('Не удалось отправить сообщение', err.detail || 'Сначала ответьте на вопрос агента', err);
                if (id === this.id()) void this.load(true);
            } else {
                this.notify.error('Не удалось отправить сообщение', describeError(err).text, err);
            }
            return false;
        }
    }

    protected async respond(request: PermissionRequest, response: PermissionResponse): Promise<void> {
        if (this.responding()) return;
        this.responding.set(response);
        const id = this.id();
        try {
            await this.api.post(`${this.base}/permissions/${encodeURIComponent(request.id)}`, { response });
            if (response !== 'reject') this.setStatus('running', id);
            if (id !== this.id()) return;
            this.permissions.update((list) => list.filter((p) => p.id !== request.id));
            void this.load(true);
        } catch (err) {
            this.notify.error('Не удалось ответить на запрос разрешения', describeError(err).text, err);
            if (id === this.id() && err instanceof ApiError && (err.isNotFound || err.isConflict)) void this.load(true);
        } finally {
            this.responding.set(null);
        }
    }

    /** Answer to the agent's question. */
    protected readonly reply = async (payload: { answers: string[][] }): Promise<boolean> => {
        const request = this.pendingQuestion();
        if (!request) return false;
        const id = this.id();
        try {
            await this.api.post(`${this.base}/questions/${encodeURIComponent(request.id)}/reply`, payload, {
                idempotencyKey: this.replyAction.keyFor({ chat: id, request: request.id, payload }),
            });
            this.replyAction.done();
            this.setStatus('running', id);
            if (id !== this.id()) return true;
            this.questions.update((list) => list.filter((q) => q.id !== request.id));
            void this.load(true);
            return true;
        } catch (err) {
            this.notify.error('Не удалось отправить ответ', describeError(err).text, err);
            if (id === this.id() && err instanceof ApiError && (err.isNotFound || err.isConflict)) void this.load(true);
            return false;
        }
    };

    protected async abort(): Promise<void> {
        if (this.aborting()) return;
        this.aborting.set(true);
        const id = this.id();
        try {
            await this.api.post(`${this.base}/abort`, {});
            this.setStatus('cancelling', id);
        } catch (err) {
            if (!(err instanceof ApiError && err.isConflict)) this.notify.error('Не удалось остановить агента', describeError(err).text, err);
            if (id === this.id()) void this.load(true);
        } finally {
            this.aborting.set(false);
        }
    }

    private scrollToEnd(): void {
        afterNextRender(
            () => {
                const el = this.scroller()?.nativeElement;
                if (el) el.scrollTop = el.scrollHeight;
            },
            { injector: this.injector },
        );
    }
}
