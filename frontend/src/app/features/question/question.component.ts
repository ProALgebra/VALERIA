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
import { KbqComponentColors, PopUpPlacements } from '@koobiq/components/core';
import { KbqIconModule } from '@koobiq/components/icon';
import { KbqTagsModule } from '@koobiq/components/tags';
import { KbqToolTipModule } from '@koobiq/components/tooltip';
import { merge } from 'rxjs';
import { ApiError } from '../../core/api/api-error';
import { ApiService } from '../../core/api/api.service';
import { EventsService } from '../../core/events/events.service';
import { ItemsStore } from '../../core/items/items.store';
import { NotifyService } from '../../core/notify/notify.service';
import { describeError, errorCodeText } from '../../core/status/error-codes';
import { isWorking, statusLabel } from '../../core/status/status';
import { ScreenHeaderComponent } from '../../shell/screen-header/screen-header.component';
import { IdempotentAction } from '../../shared/chat-composer/send-action';
import { ChatComposerComponent } from '../../shared/chat-composer/chat-composer.component';
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
import { ConsultationDetail, QueueJob, queueLine, questionTitle } from './question.model';

/** Answers given in this browser session, shown under the question they clarify (the API does not return them). */
interface Clarified {
    afterMessageId: string;
    question: string;
    answer: string;
}
const clarifiedByChat = new Map<string, Clarified[]>();

const BUSY_REASON = 'Можно будет написать, когда Valeria ответит';

/** Question screen (SPEC §8.2): one consultation with Valeria. */
@Component({
    selector: 'v-question',
    imports: [
        ScreenHeaderComponent,
        PageStateComponent,
        NotFoundComponent,
        KbqTagsModule,
        KbqIconModule,
        KbqToolTipModule,
        KbqAlertModule,
        KbqButtonModule,
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
        <v-screen-header [title]="title()" [chat]="item()">
            @if (!notFound()) {
                <kbq-tag
                    vHeaderAfterTitle
                    [color]="colors.ContrastFade"
                    [kbqTooltip]="'Valeria читает код, GitLab и YouTrack. Файлы не меняет.'"
                    [kbqPlacement]="placements.Bottom"
                    tabindex="0"
                    aria-label="Только чтение. Valeria читает код, GitLab и YouTrack. Файлы не меняет."
                >
                    <i kbq-icon="kbq-eye_16"></i>
                    Только чтение
                </kbq-tag>
            }
        </v-screen-header>

        @if (notFound()) {
            <v-not-found />
        } @else {
            <v-page-state [loading]="loading()" [error]="error()" (retry)="load()">
                <div class="v-content" #scroller>
                    <div class="v-chat">
                        @if (detail(); as d) {
                            @for (m of d.messages; track m.id; let last = $last) {
                                @if (m.role === 'user') {
                                    <v-user-message [text]="m.text" [time]="m.id === firstUserId() ? createdTime() : ''" />
                                    @for (c of clarifiedAfter(m.id); track $index) {
                                        <div class="v-clarified">
                                            <i kbq-icon="kbq-circle-question_16" class="v-clarified__icon" [color]="colors.ContrastFade" aria-hidden="true"></i>
                                            <div>
                                                <div class="v-secondary">{{ c.question }}</div>
                                                <div>Ваш ответ: <b>{{ c.answer }}</b></div>
                                            </div>
                                        </div>
                                    }
                                } @else {
                                    <v-bot-message [time]="last ? updatedTime() : ''" [copyText]="m.text">
                                        <v-markdown-view [text]="m.text" />
                                    </v-bot-message>
                                }
                            }
                            @switch (phase()) {
                                @case ('queued') {
                                    <v-status-line [text]="queueText()" [sub]="hasAnswer() ? '' : queuedSub" />
                                }
                                @case ('running') {
                                    <v-status-line text="Valeria готовит ответ" sub="Обычно 1–3 минуты." />
                                }
                                @case ('other') {
                                    <div class="v-secondary v-plain-status">{{ statusText() }}</div>
                                }
                            }
                        }
                    </div>
                </div>
                @if (detail()) {
                    <div class="v-dock">
                        <div class="v-chat">
                            @if (phase() === 'clarify' && pendingQuestion(); as q) {
                                <v-clarify-card [request]="q" [submit]="reply" />
                            } @else if (phase() === 'failed') {
                                <kbq-alert alertStyle="colored" alertColor="error" [compact]="true">
                                    <i kbq-icon="kbq-circle-exclamation_16"></i>
                                    <div kbq-alert-title>Не удалось подготовить ответ</div>
                                    Вопрос сохранён. Отправьте его ещё раз.
                                    @if (failure(); as f) {
                                        <div class="v-secondary v-failure">{{ f.text }} <span class="v-tertiary">{{ f.code }}</span></div>
                                    }
                                    <div kbq-alert-control>
                                        <v-send-button
                                            label="Отправить ещё раз"
                                            [shortcut]="false"
                                            [sending]="retrying()"
                                            (pressed)="retry()"
                                            >Отправить ещё раз</v-send-button
                                        >
                                    </div>
                                </kbq-alert>
                            } @else {
                                <v-chat-composer
                                    [draftKey]="'question:' + id()"
                                    placeholder="Задать следующий вопрос"
                                    label="Спросить"
                                    [busy]="busy()"
                                    [busyReason]="busyReason"
                                    emptyReason="Напишите вопрос"
                                    [submit]="ask"
                                />
                            }
                        </div>
                    </div>
                }
            </v-page-state>
        }
    `,
    styles: `
        :host { display: flex; flex-direction: column; min-height: 0; }
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
        .v-clarified {
            display: flex;
            gap: var(--kbq-size-s);
            align-items: flex-start;
            margin: var(--kbq-size-s) 0;
            padding: var(--kbq-size-s) var(--kbq-size-m);
            border-left: var(--kbq-size-3xs) solid var(--kbq-line-contrast-fade);
        }
        .v-clarified__icon { flex: none; margin-top: var(--kbq-size-3xs); }
        .v-plain-status { padding: var(--kbq-size-s) 0; }
        .v-failure { margin-top: var(--kbq-size-xxs); }
    `,
})
export class QuestionComponent {
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
    protected readonly busyReason = BUSY_REASON;
    protected readonly queuedSub = 'Ответ появится здесь. Valeria напишет вам в Mattermost, когда ответ будет готов.';

    protected readonly detail = signal<ConsultationDetail | null>(null);
    protected readonly loading = signal(true);
    protected readonly error = signal<unknown>(null);
    protected readonly notFound = signal(false);
    protected readonly questions = signal<QuestionRequest[]>([]);
    protected readonly job = signal<QueueJob | null>(null);
    protected readonly retrying = signal(false);
    private readonly clarified = signal<Clarified[]>([]);

    protected readonly item = computed(() => this.items.byChatId(this.id()));
    protected readonly status = computed(() => this.detail()?.status ?? '');
    protected readonly title = computed(() => (this.notFound() ? '' : questionTitle(this.detail())));
    protected readonly hasAnswer = computed(() => !!this.detail()?.messages.some((m) => m.role === 'assistant'));
    protected readonly firstUserId = computed(() => this.detail()?.messages.find((m) => m.role === 'user')?.id ?? '');
    protected readonly createdTime = computed(() => messageTime(this.detail()?.created_at));
    protected readonly updatedTime = computed(() => messageTime(this.detail()?.updated_at));
    protected readonly pendingQuestion = computed(() => this.questions()[0] ?? null);
    protected readonly queueText = computed(() => queueLine(this.job()?.status === 'queued' ? this.job()?.position : null));
    protected readonly failure = computed(() => {
        const code = this.job()?.status === 'failed' ? this.job()?.error_code : null;
        return code ? errorCodeText(code) : null;
    });
    protected readonly statusText = computed(() => statusLabel(this.status(), 'question'));

    /** What the screen shows for the current status. */
    protected readonly phase = computed<'queued' | 'running' | 'clarify' | 'failed' | 'done' | 'other'>(() => {
        const s = this.status();
        if (s === 'queued') return 'queued';
        if (s === 'dispatching' || s === 'running') return 'running';
        if (s === 'waiting_question') return 'clarify';
        if (s === 'failed') return 'failed';
        if (s === 'completed') return 'done';
        return 'other';
    });
    protected readonly busy = computed(() => isWorking(this.status()) || this.status() === 'waiting_question');

    private readonly askAction = new IdempotentAction();
    private readonly retryAction = new IdempotentAction();
    private readonly replyAction = new IdempotentAction();
    private loadSeq = 0;

    constructor() {
        effect(() => {
            const id = this.id();
            untracked(() => {
                this.detail.set(null);
                this.questions.set([]);
                this.job.set(null);
                this.notFound.set(false);
                this.clarified.set(clarifiedByChat.get(id) ?? []);
                void this.load();
            });
        });

        const destroyRef = inject(DestroyRef);
        this.events
            .on('consultation.updated')
            .pipe(takeUntilDestroyed())
            .subscribe((e) => {
                const job = e.job ?? {};
                if (String(job.chat_id) === this.id()) {
                    if (job.status) this.detail.update((d) => (d ? { ...d, status: String(job.status) } : d));
                    void this.load(true);
                } else if (this.status() === 'queued') {
                    void this.loadJob();
                }
            });
        this.events
            .on('job.updated')
            .pipe(takeUntilDestroyed())
            .subscribe(() => {
                if (this.status() === 'queued') void this.loadJob();
            });
        this.events
            .on('chat.deleted')
            .pipe(takeUntilDestroyed())
            .subscribe((e) => {
                if (String(e.chat_id) === this.id()) this.markGone();
            });
        merge(this.events.reconnected$)
            .pipe(takeUntilDestroyed())
            .subscribe(() => void this.load(true));
        destroyRef.onDestroy(this.events.pollWhileDisconnected(() => void this.load(true), 5000));
    }

    /** Loads the consultation, then what the status needs: questions or the queue row. `quiet` keeps the content. */
    async load(quiet = false): Promise<void> {
        const id = this.id();
        const seq = ++this.loadSeq;
        if (!quiet) {
            this.loading.set(true);
            this.error.set(null);
        }
        try {
            const detail = await this.api.get<ConsultationDetail>(`/api/consultations/${encodeURIComponent(id)}`);
            if (seq !== this.loadSeq) return;
            const grew = (detail.messages?.length ?? 0) !== (this.detail()?.messages.length ?? -1);
            this.detail.set({ ...detail, messages: detail.messages ?? [] });
            this.items.patchChat(id, { status: detail.status, title: detail.title || undefined });
            await Promise.all([
                detail.status === 'waiting_question' ? this.loadQuestions() : (this.questions.set([]), null),
                ['queued', 'failed'].includes(detail.status) ? this.loadJob() : (this.job.set(null), null),
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
            const list = await this.api.get<QuestionRequest[]>(`/api/consultations/${encodeURIComponent(id)}/questions`);
            if (id !== this.id()) return;
            this.questions.set(Array.isArray(list) ? list.filter((q) => q.questions?.length) : []);
        } catch (err) {
            if (id !== this.id()) return;
            this.notify.error('Не удалось загрузить уточняющий вопрос', describeError(err).text, err);
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

    protected clarifiedAfter(messageId: string): Clarified[] {
        return this.clarified().filter((c) => c.afterMessageId === messageId);
    }

    /** Follow-up question. */
    protected readonly ask = async (text: string): Promise<boolean> => {
        const id = this.id();
        const ok = await this.postMessage(text, this.askAction, 'Не удалось отправить вопрос');
        if (ok) this.appendUserMessage(id, text);
        return ok;
    };

    /** «Отправить ещё раз»: re-sends the last question. */
    protected async retry(): Promise<void> {
        const last = [...(this.detail()?.messages ?? [])].reverse().find((m) => m.role === 'user');
        if (!last || this.retrying()) return;
        this.retrying.set(true);
        const id = this.id();
        try {
            if ((await this.postMessage(last.text, this.retryAction, 'Не удалось отправить вопрос ещё раз')) && id === this.id()) {
                this.detail.update((d) => (d ? { ...d, status: 'queued' } : d));
                this.job.set(null);
                void this.load(true);
            }
        } finally {
            this.retrying.set(false);
        }
    }

    /** Answer to «Valeria уточняет». */
    protected readonly reply = async (payload: { answers: string[][] }): Promise<boolean> => {
        const request = this.pendingQuestion();
        if (!request) return false;
        const id = this.id();
        try {
            await this.api.post(
                `/api/consultations/${encodeURIComponent(id)}/questions/${encodeURIComponent(request.id)}/reply`,
                payload,
                { idempotencyKey: this.replyAction.keyFor({ chat: id, request: request.id, payload }) },
            );
            this.replyAction.done();
            // Another chat is open now: its detail is not the one the answers belong to.
            if (id !== this.id()) return true;
            const lastUser = [...(this.detail()?.messages ?? [])].reverse().find((m) => m.role === 'user');
            const records = request.questions.map((q, i) => ({
                afterMessageId: lastUser?.id ?? '',
                question: q.question,
                answer: payload.answers[i].join(', '),
            }));
            const all = [...(clarifiedByChat.get(id) ?? []), ...records];
            clarifiedByChat.set(id, all);
            this.clarified.set(all);
            this.questions.update((list) => list.filter((q) => q.id !== request.id));
            this.detail.update((d) => (d ? { ...d, status: 'running' } : d));
            void this.load(true);
            return true;
        } catch (err) {
            this.notify.error('Не удалось отправить ответ', describeError(err).text, err);
            if (id === this.id() && err instanceof ApiError && (err.isNotFound || err.isConflict)) void this.load(true);
            return false;
        }
    };

    private async postMessage(text: string, action: IdempotentAction, failTitle: string): Promise<boolean> {
        const id = this.id();
        const body = { message: text };
        try {
            await this.api.post(`/api/consultations/${encodeURIComponent(id)}/messages`, body, {
                // The chat id is part of the key: the same text in another chat is another action.
                idempotencyKey: action.keyFor({ chat: id, ...body }),
            });
            action.done();
            return true;
        } catch (err) {
            if (err instanceof ApiError && err.isConflict) {
                // «Сначала ответьте на уточняющий вопрос»: Valeria asked something meanwhile.
                this.notify.error(failTitle, err.detail || 'Сначала ответьте на уточняющий вопрос', err);
                if (id === this.id()) void this.load(true);
            } else {
                this.notify.error(failTitle, describeError(err).text, err);
            }
            return false;
        }
    }

    /** `id` is the chat the message was sent to: after the awaited POST the screen may show another chat. */
    private appendUserMessage(id: string, text: string): void {
        this.items.patchChat(id, { status: 'queued' });
        if (id !== this.id()) return;
        this.detail.update((d) =>
            d
                ? {
                      ...d,
                      status: 'queued',
                      messages: [...d.messages, { id: `local-${Date.now()}`, role: 'user', text, status: 'queued' }],
                  }
                : d,
        );
        this.scrollToEnd();
        void this.loadJob();
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
