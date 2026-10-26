import { ChangeDetectionStrategy, Component, computed, effect, inject, input, signal, untracked } from '@angular/core';
import { KbqAlertColors, KbqAlertModule } from '@koobiq/components/alert';
import { KbqBadgeModule } from '@koobiq/components/badge';
import { KbqDlModule } from '@koobiq/components/dl';
import { KbqIconModule } from '@koobiq/components/icon';
import { KbqProgressSpinnerModule } from '@koobiq/components/progress-spinner';
import { ApiError } from '../../../core/api/api-error';
import { ApiService } from '../../../core/api/api.service';
import { errorCodeText } from '../../../core/status/error-codes';
import { isWaiting, isWorking, adminStatusLabel, statusBadgeColor } from '../../../core/status/status';
import { PageStateComponent } from '../../../shared/page-state/page-state.component';
import { ScreenHeaderComponent } from '../../../shell/screen-header/screen-header.component';
import { AdminJob, AdminRequest } from '../shared/admin.models';
import { DASH, formatWhen } from '../shared/format';
import { MarkdownViewComponent } from '../../../shared/markdown-view/markdown-view.component';

/** One line of the read-only conversation. */
interface ViewMessage {
    id: string;
    role: 'user' | 'assistant';
    text: string;
    at: string | null;
}

interface TaskResult {
    status: 'available' | 'unavailable' | 'not_completed';
    text: string | null;
}

interface ConsultationDetail {
    messages: { id: string; role: 'user' | 'assistant'; text: string; status?: string }[];
    jobs?: { id: string; created_at: string }[];
}

interface OpencodeMessage {
    info: { id: string; role: 'user' | 'assistant'; time?: { created?: number } };
    parts: { type: string; text?: string }[];
}

const KIND_OBJECT: Record<AdminRequest['kind'], string> = {
    task: 'запуск',
    question: 'вопрос',
    folder: 'работу в папке',
};

/**
 * Admin's read-only view of a user's request (`/admin/requests/:id`, SPEC §10.5): a task run (details + result)
 * or a question / folder conversation. Nothing can be sent from here.
 */
@Component({
    selector: 'v-admin-request-view',
    imports: [
        ScreenHeaderComponent,
        PageStateComponent,
        KbqAlertModule,
        KbqBadgeModule,
        KbqDlModule,
        KbqIconModule,
        MarkdownViewComponent,
        KbqProgressSpinnerModule,
    ],
    changeDetection: ChangeDetectionStrategy.OnPush,
    template: `
        <v-screen-header [title]="request()?.title ?? 'Запрос'" [crumbs]="crumbs">
            <ng-container vHeaderAfterTitle>
                @if (request(); as r) {
                    <kbq-badge [badgeColor]="badgeColor(r)">{{ status(r) }}</kbq-badge>
                }
            </ng-container>
        </v-screen-header>

        <div class="v-scroll">
            <v-page-state [loading]="loading()" [error]="error()" [errorTitle]="errorTitle()" (retry)="load()">
                @if (request(); as r) {
                    <div class="v-chat">
                        <kbq-alert [compact]="true" [alertColor]="alertColors.Info" alertStyle="colored">
                            <i kbq-icon="kbq-eye_16" color="theme"></i>
                            Вы смотрите {{ kindObject(r) }} пользователя <b>{{ r.user.login || dash }}</b
                            >. Ответить или написать от его имени отсюда нельзя.
                        </kbq-alert>

                        @if (r.kind === 'task') {
                            <kbq-dl class="v-mt24" [vertical]="false">
                                <kbq-dt>Задача</kbq-dt>
                                <kbq-dd>{{ job()?.task_title || r.task_title || dash }}</kbq-dd>
                                <kbq-dt>Пользователь</kbq-dt>
                                <kbq-dd
                                    ><span class="v-mono">{{ r.user.login || dash }}</span></kbq-dd
                                >
                                <kbq-dt>Статус</kbq-dt>
                                <kbq-dd
                                    ><kbq-badge [badgeColor]="badgeColor(r)">{{ status(r) }}</kbq-badge></kbq-dd
                                >
                                <kbq-dt>Поставлена в очередь</kbq-dt>
                                <kbq-dd>{{ when(job()?.created_at ?? r.created_at) }}</kbq-dd>
                                <kbq-dt>Очередь</kbq-dt>
                                <kbq-dd>{{ job()?.queue_name || dash }}</kbq-dd>
                                <kbq-dt>Модель</kbq-dt>
                                <kbq-dd
                                    ><span class="v-mono">{{ model() }}</span></kbq-dd
                                >
                            </kbq-dl>

                            @if (r.status === 'failed') {
                                <div class="v-subheading">Ошибка</div>
                                <div class="v-runcard">{{ failure() }}</div>
                            } @else if (r.status === 'completed') {
                                <div class="v-subheading">Итог</div>
                                <div class="v-runcard">
                                    @if (result()?.text; as text) {
                                        <v-markdown-view [text]="text" />
                                    } @else {
                                        <span class="v-secondary">{{ dash }}</span>
                                    }
                                </div>
                            } @else {
                                <div class="v-status v-mt24">
                                    @if (working(r)) {
                                        <kbq-progress-spinner mode="indeterminate" size="compact" />
                                    } @else {
                                        <i kbq-icon="kbq-clock_16" class="v-fade" aria-hidden="true"></i>
                                    }
                                    <span>{{ status(r) }}. Итог появится здесь, когда задача закончится.</span>
                                </div>
                            }
                        } @else {
                            @for (m of messages(); track m.id) {
                                @if (m.role === 'user') {
                                    <div class="v-msg v-msg--me">
                                        <div class="v-msg__bubble">{{ m.text }}</div>
                                        <div class="v-msg__meta">{{ r.user.login }} · {{ when(m.at) }}</div>
                                    </div>
                                } @else {
                                    <div class="v-msg">
                                        <div class="v-msg__who">
                                            <span class="v-logo" aria-hidden="true"></span>Valeria
                                        </div>
                                        <v-markdown-view [text]="m.text" />
                                    </div>
                                }
                            }
                            @if (waiting(r)) {
                                <div class="v-waiting">
                                    <i kbq-icon="kbq-clock_16" aria-hidden="true"></i>Ждёт ответа пользователя
                                </div>
                            }
                        }
                    </div>
                }
            </v-page-state>
        </div>
    `,
    styles: `
        :host {
            display: flex;
            flex-direction: column;
            flex: 1 1 auto;
            min-height: 0;
            min-width: 0;
        }
        .v-scroll {
            flex: 1 1 auto;
            min-height: 0;
            overflow: auto;
            padding: 16px 24px 24px;
        }
        .v-chat {
            max-width: 800px;
            margin: 0 auto;
        }
        .v-mt24 {
            margin-top: 24px;
        }
        .v-mono {
            font-family: var(--kbq-font-family-mono, monospace);
        }
        .v-secondary {
            color: var(--kbq-foreground-contrast-secondary);
        }
        .v-fade {
            color: var(--kbq-icon-contrast-fade);
        }
        .v-subheading {
            margin: 24px 0 8px;
            font-family: var(--kbq-typography-text-big-strong-font-family);
            font-size: var(--kbq-typography-text-big-strong-font-size);
            line-height: var(--kbq-typography-text-big-strong-line-height);
            font-weight: var(--kbq-typography-text-big-strong-font-weight);
            letter-spacing: var(--kbq-typography-text-big-strong-letter-spacing);
        }
        .v-runcard {
            padding: 16px;
            border-radius: var(--kbq-size-m);
            border: 1px solid var(--kbq-line-contrast-less);
            background: var(--kbq-background-card);
            white-space: pre-wrap;
        }
        .v-status {
            display: flex;
            gap: 12px;
            align-items: center;
            padding: 8px 0;
            color: var(--kbq-foreground-contrast);
        }
        .v-msg {
            margin: 16px 0;
        }
        .v-msg--me {
            display: flex;
            flex-direction: column;
            align-items: flex-end;
        }
        .v-msg__bubble {
            max-width: 80%;
            padding: 10px 14px;
            border-radius: var(--kbq-size-m) var(--kbq-size-m) var(--kbq-size-xxs) var(--kbq-size-m);
            background: var(--kbq-background-theme-less);
            white-space: pre-wrap;
        }
        .v-msg__meta {
            font-size: var(--kbq-typography-text-compact-font-size);
            line-height: var(--kbq-typography-text-compact-line-height);
            color: var(--kbq-foreground-contrast-tertiary);
            margin-top: 4px;
        }
        .v-msg__who {
            display: flex;
            align-items: center;
            gap: 6px;
            font-weight: var(--kbq-typography-text-normal-strong-font-weight);
            margin-bottom: 6px;
        }
        .v-logo {
            display: inline-block;
            width: 20px;
            height: 20px;
            border-radius: var(--kbq-size-xs);
            background: var(--kbq-background-theme);
            flex: none;
        }
        .v-waiting {
            display: flex;
            align-items: center;
            gap: 8px;
            margin-top: 10px;
            color: var(--kbq-foreground-warning);
            font-weight: var(--kbq-typography-text-normal-medium-font-weight);
        }
    `,
})
export class RequestViewComponent {
    readonly id = input.required<string>();

    private readonly api = inject(ApiService);

    protected readonly alertColors = KbqAlertColors;
    protected readonly dash = DASH;
    protected readonly crumbs = [{ label: 'Запросы пользователей', link: '/admin/requests' }];

    protected readonly loading = signal(true);
    protected readonly error = signal<unknown>(null);
    protected readonly errorTitle = signal('Не удалось загрузить данные');
    protected readonly request = signal<AdminRequest | null>(null);
    protected readonly job = signal<AdminJob | null>(null);
    protected readonly result = signal<TaskResult | null>(null);
    protected readonly messages = signal<ViewMessage[]>([]);

    protected readonly model = computed(() => {
        const j = this.job();
        return j?.provider_id && j?.model_id ? `${j.provider_id}/${j.model_id}` : DASH;
    });
    protected readonly failure = computed(() => {
        const code = this.job()?.error_code;
        return code ? errorCodeText(code).text : DASH;
    });

    constructor() {
        effect(() => {
            this.id();
            untracked(() => void this.load());
        });
    }

    async load(): Promise<void> {
        const id = this.id();
        this.loading.set(true);
        this.error.set(null);
        this.errorTitle.set('Не удалось загрузить данные');
        this.job.set(null);
        this.result.set(null);
        this.messages.set([]);
        try {
            const all = await this.api.get<AdminRequest[]>('/api/admin/requests');
            const r = all.find((x) => x.id === id) ?? null;
            if (!r) {
                this.request.set(null);
                this.errorTitle.set('Запрос не найден');
                this.error.set(new ApiError(404, null, 'Возможно, его удалили.', null));
                return;
            }
            this.request.set(r);
            if (r.kind === 'task') await this.loadTask(r);
            else if (r.kind === 'question') await this.loadQuestion(r);
            else await this.loadFolder(r);
        } catch (err) {
            this.error.set(err);
        } finally {
            this.loading.set(false);
        }
    }

    private async loadTask(r: AdminRequest): Promise<void> {
        const job = await this.api.get<AdminJob>(`/api/jobs/${encodeURIComponent(r.id)}`);
        this.job.set(job);
        if (job.status === 'completed') {
            this.result.set(await this.api.get<TaskResult>(`/api/task-runs/${encodeURIComponent(r.id)}/result`));
        }
    }

    private async loadQuestion(r: AdminRequest): Promise<void> {
        const detail = await this.api.get<ConsultationDetail>(
            `/api/admin/consultations/${encodeURIComponent(r.chat_id)}`,
        );
        const created = new Map((detail.jobs ?? []).map((j) => [j.id, j.created_at]));
        this.messages.set(
            detail.messages
                .filter((m) => m.text)
                .map((m) => ({
                    id: m.id,
                    role: m.role,
                    text: m.text,
                    at: m.role === 'user' ? (created.get(m.id) ?? null) : null,
                })),
        );
    }

    private async loadFolder(r: AdminRequest): Promise<void> {
        const raw = await this.api.get<OpencodeMessage[]>(`/api/chats/${encodeURIComponent(r.chat_id)}/messages`);
        this.messages.set(
            raw
                .map((m) => ({
                    id: m.info.id,
                    role: m.info.role,
                    text: m.parts
                        .filter((p) => p.type === 'text' && p.text)
                        .map((p) => p.text)
                        .join('\n\n'),
                    at: m.info.time?.created ? new Date(m.info.time.created).toISOString() : null,
                }))
                .filter((m) => m.text),
        );
    }

    protected kindObject(r: AdminRequest): string {
        return KIND_OBJECT[r.kind] ?? 'запрос';
    }

    protected status(r: AdminRequest): string {
        return adminStatusLabel(r.status, r.kind);
    }

    protected badgeColor(r: AdminRequest): ReturnType<typeof statusBadgeColor> {
        return statusBadgeColor(r.status, r.kind);
    }

    protected working(r: AdminRequest): boolean {
        return isWorking(r.status);
    }

    protected waiting(r: AdminRequest): boolean {
        return isWaiting(r.status);
    }

    protected when(iso: string | null): string {
        return formatWhen(iso);
    }
}
