import { ChangeDetectionStrategy, Component, computed, DestroyRef, inject, signal } from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { KbqBadgeModule } from '@koobiq/components/badge';
import { KbqButtonModule } from '@koobiq/components/button';
import { KbqComponentColors } from '@koobiq/components/core';
import { KbqModalRef, KbqModalService, ModalSize } from '@koobiq/components/modal';
import { EventsService } from '../../../core/events/events.service';
import { NotifyService } from '../../../core/notify/notify.service';
import { describeError } from '../../../core/status/error-codes';
import { PageStateComponent } from '../../../shared/page-state/page-state.component';
import { ScreenHeaderComponent } from '../../../shell/screen-header/screen-header.component';
import { AdminConfigApi, QueueOverview } from '../config-shared/admin-config.api';
import { escapeHtml } from '../config-shared/format';
import { ADMIN_GRID } from '../shared/admin-grid';
import { LiveRow, liveRows, liveStats, stopText, stopTitle } from './queue-live';
import { QueueTabsComponent } from './queue-tabs.component';

/** The backend has no job event in SSE, so the page polls; a reconnect also triggers a re-read. */
export const POLL_MS = 5000;

/** «Очередь» → «Сейчас» (SPEC §10.3): load, three counters and the unfinished jobs with «Остановить» / «Отменить». */
@Component({
    selector: 'v-queue-live',
    imports: [ScreenHeaderComponent, PageStateComponent, QueueTabsComponent, ADMIN_GRID, KbqBadgeModule, KbqButtonModule],
    changeDetection: ChangeDetectionStrategy.OnPush,
    template: `
        <v-screen-header title="Очередь" />
        <v-page-state [loading]="!data() && !error()" [error]="data() ? null : error()" (retry)="load()">
            <div class="v-content">
                <v-queue-tabs active="live" />
                @if (stats(); as s) {
                    <div class="v-stats">
                        <div class="v-stat">
                            <div class="v-stat__t">Активные сессии</div>
                            <div class="v-stat__v">{{ s.active }} из {{ s.limit }}</div>
                            <div class="v-stat__s">общий лимит</div>
                        </div>
                        <div class="v-stat">
                            <div class="v-stat__t">В очереди</div>
                            <div class="v-stat__v">{{ s.queued }}</div>
                            <div class="v-stat__s">{{ s.longestWait ?? '—' }}</div>
                        </div>
                        <div class="v-stat">
                            <div class="v-stat__t">Ждут пользователя</div>
                            <div class="v-stat__v">{{ s.waitingUser }}</div>
                            <div class="v-stat__s">не занимают слот</div>
                        </div>
                    </div>
                }
                <v-admin-grid
                    class="v-jobs"
                    [rows]="rows()"
                    [rowId]="rowId"
                    noRowsText="Сейчас ничего не выполняется и никто не ждёт"
                >
                    <ng-template vCol="n" header="№" kind="nowrap" let-r>
                        <span class="v-secondary">{{ r.n }}</span>
                    </ng-template>
                    <ng-template vCol="user" header="Пользователь" kind="nowrap" let-r>{{ r.login }}</ng-template>
                    <ng-template vCol="task" header="Задание" [flex]="2" [minWidth]="200" let-r>
                        {{ r.title }}
                        <span class="v-sub">{{ r.kindLabel }}</span>
                    </ng-template>
                    <ng-template vCol="queue" header="Очередь" [flex]="1" [minWidth]="120" let-r>{{ r.queue }}</ng-template>
                    <ng-template vCol="status" header="Статус" kind="line" let-r>
                        <kbq-badge [badgeColor]="r.badge">{{ r.statusLabel }}</kbq-badge>
                    </ng-template>
                    <ng-template vCol="time" header="Время" [flex]="1" [minWidth]="140" let-r>
                        <span [class.v-warning]="r.long" [class.v-secondary]="!r.long">{{ r.time }}</span>
                    </ng-template>
                    <ng-template vCol="actions" header="Действия" kind="action" [width]="136" let-r>
                        @if (r.action === 'cancel') {
                            <button kbq-button [color]="colors.ContrastFade" [disabled]="busy().has(r.id)" (click)="cancel(r)">Отменить</button>
                        } @else if (r.action === 'stop') {
                            <button kbq-button [color]="colors.ContrastFade" [disabled]="busy().has(r.id)" (click)="askStop(r)">
                                Остановить
                            </button>
                        }
                    </ng-template>
                </v-admin-grid>
            </div>
        </v-page-state>
    `,
    styles: [
        `
        :host { display: flex; flex-direction: column; flex: 1 1 auto; min-height: 0; }
        .v-content { flex: 1 1 auto; min-height: 0; overflow: auto; padding: var(--kbq-size-l) var(--kbq-size-xxl) var(--kbq-size-xxl); }
        .v-stats { display: grid; grid-template-columns: repeat(3, 1fr); gap: var(--kbq-size-m); }
        .v-stat {
            padding: var(--kbq-size-m) var(--kbq-size-l);
            border: 1px solid var(--kbq-line-contrast-less);
            border-radius: var(--kbq-size-m);
            background: var(--kbq-background-card);
        }
        .v-stat__t { color: var(--kbq-foreground-contrast-secondary); }
        .v-stat__v {
            font-size: var(--kbq-typography-headline-font-size);
            line-height: var(--kbq-typography-headline-line-height);
            font-weight: var(--kbq-typography-headline-font-weight);
        }
        .v-stat__s { color: var(--kbq-foreground-contrast-tertiary); }
        .v-jobs { margin-top: var(--kbq-size-l); }
        .v-secondary { color: var(--kbq-foreground-contrast-secondary); }
        .v-warning { color: var(--kbq-foreground-warning); }
    `,
    ],
})
export class QueueLiveComponent {
    private readonly api = inject(AdminConfigApi);
    private readonly modal = inject(KbqModalService);
    private readonly notify = inject(NotifyService);

    protected readonly colors = KbqComponentColors;
    protected readonly data = signal<QueueOverview | null>(null);
    protected readonly error = signal<unknown>(null);
    protected readonly now = signal(Date.now());
    protected readonly busy = signal<ReadonlySet<string>>(new Set());
    protected readonly rows = computed(() => (this.data() ? liveRows(this.data()!, this.now()) : []));
    protected readonly rowId = (r: LiveRow): string => r.id;
    protected readonly stats = computed(() => (this.data() ? liveStats(this.data()!, this.now()) : null));

    private seq = 0;

    constructor() {
        void this.load();
        const timer = setInterval(() => {
            if (typeof document === 'undefined' || document.visibilityState === 'visible') void this.load();
        }, POLL_MS);
        inject(DestroyRef).onDestroy(() => clearInterval(timer));
        inject(EventsService)
            .reconnected$.pipe(takeUntilDestroyed())
            .subscribe(() => void this.load());
    }

    async load(): Promise<void> {
        const seq = ++this.seq;
        try {
            const ov = await this.api.queue();
            if (seq !== this.seq) return;
            this.data.set(ov);
            this.now.set(Date.now());
            this.error.set(null);
        } catch (err) {
            if (seq === this.seq) this.error.set(err);
        }
    }

    protected askStop(r: LiveRow): void {
        const ref: KbqModalRef = this.modal.create({
            kbqTitle: stopTitle(r.kind),
            // Rendered as HTML by the modal: titles and logins come from users.
            kbqContent: escapeHtml(stopText(r)),
            kbqSize: ModalSize.Small,
            kbqClosable: true,
            kbqFooter: [
                {
                    label: 'Остановить',
                    // Filled contrast: kbq-button has no error colour, so `Error` rendered a plain grey button.
                    type: KbqComponentColors.Contrast,
                    onClick: (): void => {
                        ref.close();
                        void this.stop(r);
                    },
                },
                { label: 'Не останавливать', autoFocus: true, onClick: (): void => ref.close() },
            ],
        });
    }

    protected cancel(r: LiveRow): Promise<void> {
        return this.stop(r);
    }

    private async stop(r: LiveRow): Promise<void> {
        this.setBusy(r.id, true);
        try {
            await this.api.stopJob(r.id);
            if (r.action === 'stop') this.notify.success('Остановка запрошена', `Сессия ${r.login} остановится в течение минуты`);
            await this.load();
        } catch (err) {
            const e = describeError(err);
            this.notify.error(r.action === 'stop' ? 'Не удалось остановить' : 'Не удалось отменить', e.text, err);
        } finally {
            this.setBusy(r.id, false);
        }
    }

    private setBusy(id: string, on: boolean): void {
        const next = new Set(this.busy());
        if (on) next.add(id);
        else next.delete(id);
        this.busy.set(next);
    }
}
