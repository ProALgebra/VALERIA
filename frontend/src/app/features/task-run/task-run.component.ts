import {
    ChangeDetectionStrategy,
    Component,
    computed,
    DestroyRef,
    effect,
    inject,
    input,
    signal,
    TemplateRef,
    untracked,
    viewChild,
} from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { Router } from '@angular/router';
import { KbqAlertModule } from '@koobiq/components/alert';
import { KbqBadgeModule } from '@koobiq/components/badge';
import { KbqButtonModule } from '@koobiq/components/button';
import { KbqComponentColors, PopUpPlacements } from '@koobiq/components/core';
import { KbqDlModule } from '@koobiq/components/dl';
import { KbqIconModule } from '@koobiq/components/icon';
import { KbqModalRef, KbqModalService, ModalSize } from '@koobiq/components/modal';
import { KbqProgressSpinnerModule } from '@koobiq/components/progress-spinner';
import { ApiError } from '../../core/api/api-error';
import { ApiService } from '../../core/api/api.service';
import { AuthService } from '../../core/auth/auth.service';
import { EventsService } from '../../core/events/events.service';
import { ItemsStore } from '../../core/items/items.store';
import { NotifyService } from '../../core/notify/notify.service';
import { errorCodeText } from '../../core/status/error-codes';
import { isWorking, isWaiting, statusBadgeColor, statusLabel } from '../../core/status/status';
import { ChatAsideComponent } from '../../shell/aside/chat-aside.component';
import { ScreenHeaderComponent } from '../../shell/screen-header/screen-header.component';
import { NotFoundComponent } from '../../shared/not-found/not-found.component';
import { PageStateComponent } from '../../shared/page-state/page-state.component';
import { failureText } from '../../shared/chat-composer/send-action';
import { MarkdownViewComponent } from '../../shared/markdown-view/markdown-view.component';
import { clockTime, sameId, TaskRun, TaskRunResult } from '../../shared/tasks/task.model';
import { TasksCatalog } from '../../shared/tasks/tasks-catalog.service';
import { HomeDrafts } from '../home/home-drafts';
import { NOTIFY_TEXT } from '../home/home.component';
import { canCancel, durationText, minutesText, pluralTasks, runStages, startedText } from './run-view';

interface QueueRow {
    id: string | number;
    status: string;
    position: number | null;
}

const FAILED_HINT =
    'Проверьте, открывается ли ссылка в дополнении, и запустите ещё раз. Подробности запуска есть у администратора.';

/**
 * Task run (`/tasks/runs/:id`), SPEC §8.3: stages, waiting / working card, the final report or the error,
 * cancel with confirmation, «Запустить ещё раз» with the same addition. The aside shows the run facts.
 * Live status comes from SSE `task.updated` / `task.title.updated`; polling while SSE is down.
 */
@Component({
    selector: 'v-task-run',
    imports: [
        ScreenHeaderComponent,
        ChatAsideComponent,
        NotFoundComponent,
        PageStateComponent,
        KbqAlertModule,
        KbqBadgeModule,
        KbqButtonModule,
        KbqDlModule,
        KbqIconModule,
        MarkdownViewComponent,
        KbqProgressSpinnerModule,
    ],
    changeDetection: ChangeDetectionStrategy.OnPush,
    host: { class: 'v-task-run' },
    templateUrl: './task-run.component.html',
    styleUrls: ['./task-run.component.scss'],
})
export class TaskRunComponent {
    readonly id = input.required<string>();

    private readonly api = inject(ApiService);
    private readonly auth = inject(AuthService);
    private readonly events = inject(EventsService);
    private readonly items = inject(ItemsStore);
    private readonly notify = inject(NotifyService);
    private readonly modal = inject(KbqModalService);
    private readonly router = inject(Router);
    private readonly catalog = inject(TasksCatalog);
    private readonly drafts = inject(HomeDrafts);
    private readonly destroyRef = inject(DestroyRef);

    protected readonly colors = KbqComponentColors;
    protected readonly placements = PopUpPlacements;
    protected readonly notifyText = NOTIFY_TEXT;
    protected readonly failedHint = FAILED_HINT;

    protected readonly run = signal<TaskRun | null>(null);
    protected readonly loading = signal(true);
    protected readonly error = signal<unknown>(null);
    protected readonly notFound = signal(false);
    protected readonly result = signal<TaskRunResult | null>(null);
    protected readonly position = signal<number | null>(null);
    protected readonly stopping = signal(false);
    private readonly now = signal(Date.now());
    private readonly cancelFooter = viewChild.required<TemplateRef<object>>('cancelFooter');
    private cancelRef: KbqModalRef | null = null;
    /** Chat of the shown run from the sidebar row, kept after the sidebar drops it (the run payload has no `chat_id`). */
    private chatOf: { run: string; chat: string | number } | null = null;

    protected readonly item = computed(() => {
        const id = this.id();
        return this.items.find((i) => i.kind === 'task' && sameId(i.id, id)) ?? null;
    });
    protected readonly taskTitle = computed(() => {
        const run = this.run();
        if (!run) return '';
        return this.catalog.find(run.task_id)?.title ?? run.title;
    });
    protected readonly status = computed(() => this.run()?.status ?? '');
    protected readonly statusText = computed(() => statusLabel(this.status(), 'task'));
    protected readonly statusColor = computed(() => statusBadgeColor(this.status(), 'task'));
    protected readonly stages = computed(() => {
        const run = this.run();
        return run ? runStages(run) : [];
    });
    protected readonly view = computed<'queued' | 'running' | 'done' | 'failed' | 'cancelled' | 'none'>(() => {
        const s = this.status();
        if (s === 'queued' || s === 'dispatching') return 'queued';
        if (s === 'running' || s === 'cancelling' || isWaiting(s)) return 'running';
        if (s === 'completed') return 'done';
        if (s === 'failed') return 'failed';
        if (s === 'cancelled') return 'cancelled';
        return 'none';
    });
    protected readonly cancellable = computed(() => canCancel(this.status()));
    protected readonly ahead = computed(() => {
        const p = this.position();
        return p === null ? null : Math.max(0, p - 1);
    });
    protected readonly aheadText = computed(() => {
        const n = this.ahead();
        if (n === null || n === 0) return 'Задача следующая в очереди';
        return `Перед вами ${n} ${pluralTasks(n)}`;
    });
    protected readonly workingText = computed(() => {
        const run = this.run();
        const started = run?.started_at ? Date.parse(run.started_at) : NaN;
        if (Number.isNaN(started)) return 'Valeria работает';
        return `Valeria работает ${minutesText(this.now() - started)}`;
    });
    protected readonly errorText = computed(() => errorCodeText(this.run()?.error_code));
    protected readonly finishedAt = computed(() => clockTime(this.run()?.finished_at));
    protected readonly startedText = computed(() => startedText(this.run()?.created_at, this.now()));
    protected readonly duration = computed(() => {
        const run = this.run();
        return run && run.status === 'completed' ? durationText(run) : '';
    });

    constructor() {
        effect(() => {
            const id = this.id();
            untracked(() => void this.load(id));
        });
        effect(() => {
            const chat = this.item()?.chat_id;
            if (chat != null) this.chatOf = { run: this.id(), chat };
        });
        void this.catalog.ensure().catch(() => undefined);

        this.events
            .on('chat.deleted')
            .pipe(takeUntilDestroyed())
            .subscribe((event) => {
                const shown = this.chatOf;
                if (shown && shown.run === this.id()) {
                    if (sameId(event.chat_id, shown.chat)) this.markGone();
                    return;
                }
                // The run payload has no chat_id and the sidebar never had this run: ask the API, which answers 404
                // once the run's chat is deleted.
                void this.checkGone();
            });

        this.events
            .on('task.updated')
            .pipe(takeUntilDestroyed())
            .subscribe((event) => {
                const current = this.run();
                if (!current || !sameId(event.task?.id, current.id)) return;
                this.apply({ ...current, ...(event.task as Partial<TaskRun>) });
            });
        this.events
            .on('task.title.updated')
            .pipe(takeUntilDestroyed())
            .subscribe((event) => {
                const current = this.run();
                if (current && sameId(event.run_id, current.id)) this.run.set({ ...current, title: event.title });
            });
        this.events.reconnected$.pipe(takeUntilDestroyed()).subscribe(() => void this.refresh());
        const stopPolling = this.events.pollWhileDisconnected(() => {
            const s = this.status();
            if (isWorking(s) || isWaiting(s)) void this.refresh();
        }, 5000);
        const ticker = setInterval(() => this.now.set(Date.now()), 30_000);
        this.destroyRef.onDestroy(() => {
            stopPolling();
            clearInterval(ticker);
        });
    }

    protected async load(id: string): Promise<void> {
        this.loading.set(true);
        this.error.set(null);
        this.notFound.set(false);
        this.result.set(null);
        this.position.set(null);
        try {
            const run = await this.api.get<TaskRun>(`/api/task-runs/${encodeURIComponent(id)}`);
            if (id !== this.id()) return;
            this.run.set(null);
            this.apply(run);
        } catch (err) {
            if (id !== this.id()) return;
            this.run.set(null);
            if (err instanceof ApiError && err.isNotFound) this.notFound.set(true);
            else this.error.set(err);
        } finally {
            if (id === this.id()) this.loading.set(false);
        }
    }

    protected confirmCancel(): void {
        const run = this.run();
        if (!run || this.stopping()) return;
        this.cancelRef = this.modal.create({
            kbqTitle: 'Отменить запуск?',
            kbqContent: `Valeria прервёт задачу «${escapeHtml(this.taskTitle())}». Итога не будет, запуск останется в списке со статусом «Отменена».`,
            kbqSize: ModalSize.Small,
            kbqClosable: true,
            // A template (not button options) so the filled error button gets this component's styles.
            kbqFooter: this.cancelFooter(),
        });
        // The safe choice gets focus, as in the prototype.
        this.cancelRef.afterOpen.subscribe(() => document.querySelector<HTMLElement>('.v-task-run__keep')?.focus());
        this.cancelRef.afterClose.subscribe(() => (this.cancelRef = null));
    }

    protected async confirmStop(): Promise<void> {
        const run = this.run();
        if (run) await this.stop(run);
        this.cancelRef?.close();
    }

    protected closeCancel(): void {
        this.cancelRef?.close();
    }

    protected rerun(): void {
        const run = this.run();
        if (!run) return;
        this.drafts.setAddition(run.task_id, run.user_input ?? '');
        void this.router.navigate(['/new', run.task_id]);
    }

    protected async copyResult(): Promise<void> {
        const text = this.result()?.text ?? '';
        try {
            await navigator.clipboard.writeText(text);
            this.notify.info('Скопировано');
        } catch {
            this.notify.error('Не удалось скопировать');
        }
    }

    private async stop(run: TaskRun): Promise<void> {
        this.stopping.set(true);
        try {
            await this.api.post(`/api/task-runs/${encodeURIComponent(String(run.id))}/stop`);
            this.notify.info('Запуск отменён');
            void this.refresh();
        } catch (error) {
            this.notify.error(failureText('Не удалось отменить запуск', error), undefined, error);
        } finally {
            this.stopping.set(false);
        }
    }

    /** The run's chat was deleted (here or in another tab): show «not found». */
    private markGone(): void {
        this.cancelRef?.close();
        this.loading.set(false);
        this.error.set(null);
        this.run.set(null);
        this.notFound.set(true);
    }

    private async checkGone(): Promise<void> {
        const id = this.id();
        if (this.notFound()) return;
        try {
            await this.api.get<TaskRun>(`/api/task-runs/${encodeURIComponent(id)}`);
        } catch (err) {
            if (id === this.id() && err instanceof ApiError && err.isNotFound) this.markGone();
        }
    }

    private async refresh(): Promise<void> {
        const id = this.id();
        try {
            const run = await this.api.get<TaskRun>(`/api/task-runs/${encodeURIComponent(id)}`);
            if (id === this.id() && !this.notFound()) this.apply(run);
        } catch {
            /* keep what is shown; the next event or poll will retry */
        }
    }

    /** Sets the run and loads what the new status needs: queue position or the result. */
    private apply(run: TaskRun): void {
        const previous = this.run();
        this.run.set(run);
        if (run.status === 'queued' && (previous?.status !== 'queued' || this.position() === null)) {
            void this.loadPosition(run.id);
        }
        if (run.status === 'completed' && (previous?.status !== 'completed' || !this.result())) {
            void this.loadResult(run.id);
        }
    }

    private async loadPosition(id: string | number): Promise<void> {
        try {
            const rows = await this.api.get<QueueRow[]>('/api/queue/me');
            const row = (Array.isArray(rows) ? rows : []).find((r) => sameId(r.id, id));
            if (sameId(this.run()?.id, id)) this.position.set(row?.position ?? null);
        } catch {
            this.position.set(null);
        }
    }

    private async loadResult(id: string | number): Promise<void> {
        try {
            const res = await this.api.get<TaskRunResult>(`/api/task-runs/${encodeURIComponent(String(id))}/result`);
            if (sameId(this.run()?.id, id)) this.result.set(res);
        } catch {
            if (sameId(this.run()?.id, id)) this.result.set({ run_id: id, status: 'unavailable', text: null });
        }
    }
}

function escapeHtml(value: string): string {
    return value.replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]!);
}
