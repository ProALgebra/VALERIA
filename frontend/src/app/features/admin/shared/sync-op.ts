import { ChangeDetectionStrategy, Component, DestroyRef, inject, input, signal } from '@angular/core';
import { KbqAlertColors, KbqAlertModule } from '@koobiq/components/alert';
import { KbqButtonModule } from '@koobiq/components/button';
import { KbqComponentColors } from '@koobiq/components/core';
import { KbqIconModule } from '@koobiq/components/icon';

/** PT UX «Синхронная форма»: after this long the form shows the slow-operation warning. */
export const SLOW_OPERATION_MS = 30_000;

export type SyncResult<T> = { ok: true; value: T } | { ok: false; error: unknown } | { ok: false; cancelled: true };

/**
 * One submit of a synchronous form: `busy` while it runs, `slow` after 30 s.
 * «Подождать еще» restarts the 30 s timer, «Отменить» returns the form to editing and drops the late result
 * (the request itself is not aborted: the server may already be applying it).
 * Create in an injection context (it clears its timer on destroy).
 */
export class SyncOp {
    readonly busy = signal(false);
    readonly slow = signal(false);
    private run_ = 0;
    private timer: ReturnType<typeof setTimeout> | null = null;

    constructor(private readonly delayMs = SLOW_OPERATION_MS) {
        inject(DestroyRef).onDestroy(() => this.clear());
    }

    async run<T>(fn: () => Promise<T>): Promise<SyncResult<T>> {
        const id = ++this.run_;
        this.busy.set(true);
        this.slow.set(false);
        this.arm();
        try {
            const value = await fn();
            return id === this.run_ ? { ok: true, value } : { ok: false, cancelled: true };
        } catch (error) {
            return id === this.run_ ? { ok: false, error } : { ok: false, cancelled: true };
        } finally {
            if (id === this.run_) this.finish();
        }
    }

    /** «Подождать еще». */
    wait(): void {
        this.slow.set(false);
        this.arm();
    }

    /** «Отменить». */
    cancel(): void {
        this.run_++;
        this.finish();
    }

    private arm(): void {
        this.clear();
        this.timer = setTimeout(() => this.slow.set(true), this.delayMs);
    }

    private finish(): void {
        this.clear();
        this.busy.set(false);
        this.slow.set(false);
    }

    private clear(): void {
        if (this.timer) clearTimeout(this.timer);
        this.timer = null;
    }
}

/** Warning alert over the form fields while `op.slow()`. */
@Component({
    selector: 'v-sync-slow-alert',
    imports: [KbqAlertModule, KbqButtonModule, KbqIconModule],
    changeDetection: ChangeDetectionStrategy.OnPush,
    template: `
        @if (op().slow()) {
            <kbq-alert [compact]="true" [alertColor]="alertColors.Warning" alertStyle="colored" role="status">
                <i kbq-icon="kbq-triangle-exclamation_16" color="warning"></i>
                Операция занимает больше времени, чем ожидалось
                <div kbq-alert-control>
                    <button kbq-button [color]="colors.ContrastFade" (click)="op().wait()">Подождать еще</button>
                    <button kbq-button [color]="colors.Contrast" kbqStyle="transparent" (click)="op().cancel()">
                        Отменить
                    </button>
                </div>
            </kbq-alert>
        }
    `,
    styles: `
        :host {
            display: contents;
        }
    `,
})
export class SyncSlowAlertComponent {
    readonly op = input.required<SyncOp>();
    protected readonly alertColors = KbqAlertColors;
    protected readonly colors = KbqComponentColors;
}
