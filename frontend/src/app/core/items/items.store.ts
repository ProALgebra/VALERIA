import { computed, DestroyRef, inject, Injectable, signal } from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { Subject } from 'rxjs';
import { ApiError, isAccessChanged } from '../api/api-error';
import { EventsService } from '../events/events.service';
import { isActive, isWaiting } from '../status/status';
import { Item } from './item.model';
import { groupItems, nextSnoozeReturn } from './items-grouping';
import { ItemsApi } from './items.api';

/** A status change seen from SSE; the shell turns final ones into toasts. */
export interface ItemTransition {
    item: Item;
    from: string;
    to: string;
}

const MAX_TIMEOUT = 2_147_483_000;

/**
 * All of the user's chats as one signal list (questions, task runs, folder chats).
 * Groups for the menu are derived; SSE events patch the list and a debounced re-read keeps it exact.
 */
@Injectable({ providedIn: 'root' })
export class ItemsStore {
    private readonly api = inject(ItemsApi);
    private readonly events = inject(EventsService);
    private readonly destroyRef = inject(DestroyRef);

    private readonly _items = signal<Item[]>([]);
    private readonly _loaded = signal(false);
    private readonly _error = signal<ApiError | null>(null);
    /** Clock used for snooze grouping; moved forward by the snooze timer. */
    private readonly now = signal(Date.now());
    private snoozeTimer: ReturnType<typeof setTimeout> | null = null;
    private refreshTimer: ReturnType<typeof setTimeout> | null = null;
    private inflight: Promise<void> | null = null;
    private started = false;

    readonly items = this._items.asReadonly();
    readonly loaded = this._loaded.asReadonly();
    readonly error = this._error.asReadonly();
    readonly groups = computed(() => groupItems(this._items(), this.now()));
    readonly transitions$ = new Subject<ItemTransition>();

    constructor() {
        const destroyRef = this.destroyRef;
        destroyRef.onDestroy(() => {
            if (this.snoozeTimer) clearTimeout(this.snoozeTimer);
            if (this.refreshTimer) clearTimeout(this.refreshTimer);
        });
        if (typeof document !== 'undefined') {
            const onVisible = () => this.tick();
            document.addEventListener('visibilitychange', onVisible);
            destroyRef.onDestroy(() => document.removeEventListener('visibilitychange', onVisible));
        }
    }

    /** Loads the list and starts listening to SSE. Idempotent. */
    start(): Promise<void> {
        if (!this.started) {
            this.started = true;
            this.listen();
        }
        return this.refresh();
    }

    /** Re-reads `/api/me/items`. Concurrent calls share one request. */
    refresh(): Promise<void> {
        if (this.inflight) return this.inflight;
        this.inflight = this.api
            .list()
            .then((items) => {
                this.setItems(items);
                this._error.set(null);
                this._loaded.set(true);
            })
            .catch((error: unknown) => {
                // A 403 that changed the user's access is not shown (SPEC §16.2).
                if (isAccessChanged(error)) return;
                this._error.set(error instanceof ApiError ? error : new ApiError(0, null, null, null));
            })
            .finally(() => {
                this.inflight = null;
            });
        return this.inflight;
    }

    find(predicate: (item: Item) => boolean): Item | undefined {
        return this._items().find(predicate);
    }

    byChatId(chatId: string | number): Item | undefined {
        return this.find((i) => same(i.chat_id, chatId));
    }

    // ---- Chat actions ----

    async archive(item: Item): Promise<void> {
        const res = await this.api.archive(item.chat_id);
        this.patchChat(item.chat_id, res ?? { archived_at: new Date().toISOString(), snoozed_until: null });
    }

    async unarchive(item: Item): Promise<void> {
        const res = await this.api.unarchive(item.chat_id);
        this.patchChat(item.chat_id, res ?? { archived_at: null });
    }

    async snooze(item: Item, until: string): Promise<void> {
        const res = await this.api.snooze(item.chat_id, until);
        this.patchChat(item.chat_id, res ?? { snoozed_until: until });
    }

    async unsnooze(item: Item): Promise<void> {
        const res = await this.api.unsnooze(item.chat_id);
        this.patchChat(item.chat_id, res ?? { snoozed_until: null });
    }

    /** A repeated delete answers 404: the chat is gone either way, so that counts as success. */
    async remove(item: Item): Promise<void> {
        try {
            await this.api.remove(item.chat_id);
        } catch (err) {
            if (!(err instanceof ApiError && err.isNotFound)) throw err;
        }
        this.removeChat(item.chat_id);
    }

    // ---- Local updates (also used by tests) ----

    setItems(items: Item[]): void {
        const before = this._items();
        this._items.set(items);
        if (this._loaded()) this.emitTransitions(before, items);
        this.tick();
    }

    /** Patches every item of a chat. Returns false if the chat is unknown. */
    patchChat(chatId: string | number, patch: Partial<Item>): boolean {
        return this.patch((i) => same(i.chat_id, chatId), patch);
    }

    removeChat(chatId: string | number): void {
        this._items.update((items) => items.filter((i) => !same(i.chat_id, chatId)));
        this.tick();
    }

    /** Moves the snooze clock to now and schedules the next return (browser local time). */
    tick(at: number = Date.now()): void {
        this.now.set(at);
        if (this.snoozeTimer) clearTimeout(this.snoozeTimer);
        this.snoozeTimer = null;
        const next = nextSnoozeReturn(this._items(), at);
        if (next !== null) {
            const delay = Math.min(Math.max(next - at + 50, 0), MAX_TIMEOUT);
            this.snoozeTimer = setTimeout(() => this.tick(), delay);
        }
    }

    private patch(predicate: (item: Item) => boolean, patch: Partial<Item>): boolean {
        let found = false;
        const before = this._items();
        const next = before.map((i) => {
            if (!predicate(i)) return i;
            found = true;
            const cleaned = Object.fromEntries(Object.entries(patch).filter(([, v]) => v !== undefined)) as Partial<Item>;
            return { ...i, ...cleaned };
        });
        if (found) {
            this._items.set(next);
            this.emitTransitions(before, next);
            this.tick();
        }
        return found;
    }

    private emitTransitions(before: Item[], after: Item[]): void {
        const prev = new Map(before.map((i) => [`${i.kind}:${i.id}`, i.status]));
        for (const item of after) {
            const from = prev.get(`${item.kind}:${item.id}`);
            if (from === undefined || from === item.status) continue;
            const becameFinal = isActive(from) && !isActive(item.status);
            const becameWaiting = !isWaiting(from) && isWaiting(item.status);
            if (becameFinal || becameWaiting) this.transitions$.next({ item, from, to: item.status });
        }
    }

    private listen(): void {
        const ev = this.events;
        ev.on('chat.updated')
            .pipe(takeUntilDestroyed(this.destroyRef))
            .subscribe((e) => {
                const patch: Partial<Item> = {};
                if ('title' in e) patch.title = e.title;
                if ('archived_at' in e) patch.archived_at = e.archived_at ?? null;
                if ('snoozed_until' in e) patch.snoozed_until = e.snoozed_until ?? null;
                if (e.status) patch.status = e.status;
                if (!this.patchChat(e.chat_id, patch)) this.scheduleRefresh();
            });
        ev.on('chat.deleted')
            .pipe(takeUntilDestroyed(this.destroyRef))
            .subscribe((e) => this.removeChat(e.chat_id));
        ev.on('consultation.updated')
            .pipe(takeUntilDestroyed(this.destroyRef))
            .subscribe((e) => {
                const job = e.job ?? {};
                const chatId = job.chat_id;
                const found =
                    chatId !== undefined &&
                    this.patch((i) => i.kind === 'question' && same(i.chat_id, chatId), pick(job, ['status', 'title']));
                if (!found) this.scheduleRefresh();
            });
        // Folder chats: the backend also sends `chat.updated` on every folder transition (RECONCILE); `job.updated`
        // is the fallback. An unfinished job is the chat status, anything else means the agent is free (`idle`).
        ev.on('job.updated')
            .pipe(takeUntilDestroyed(this.destroyRef))
            .subscribe((e) => {
                const job = (e['job'] ?? {}) as { chat_id?: string | number; status?: string };
                if (job.chat_id === undefined || !job.status) return;
                const status = isActive(job.status) ? job.status : 'idle';
                this.patch((i) => i.kind === 'folder' && same(i.chat_id, job.chat_id), { status });
            });
        ev.on('task.updated')
            .pipe(takeUntilDestroyed(this.destroyRef))
            .subscribe((e) => {
                const task = e.task;
                const found =
                    !!task &&
                    this.patch((i) => i.kind === 'task' && same(i.id, task.id), pick(task, ['status', 'title', 'phase']));
                if (!found) this.scheduleRefresh();
            });
        ev.on('task.title.updated')
            .pipe(takeUntilDestroyed(this.destroyRef))
            .subscribe((e) => {
                const found = this.patch(
                    (i) => (i.kind === 'task' && same(i.id, e.run_id)) || (e.chat_id !== undefined && same(i.chat_id, e.chat_id)),
                    { title: e.title },
                );
                if (!found) this.scheduleRefresh();
            });
        ev.reconnected$.pipe(takeUntilDestroyed(this.destroyRef)).subscribe(() => void this.refresh());
        this.destroyRef.onDestroy(ev.pollWhileDisconnected(() => void this.refresh(), 5000));
    }

    private scheduleRefresh(): void {
        if (this.refreshTimer) return;
        this.refreshTimer = setTimeout(() => {
            this.refreshTimer = null;
            void this.refresh();
        }, 300);
    }
}

function same(a: string | number | null | undefined, b: string | number | null | undefined): boolean {
    return a !== null && a !== undefined && b !== null && b !== undefined && String(a) === String(b);
}

function pick(source: Record<string, unknown>, keys: (keyof Item)[]): Partial<Item> {
    const out: Record<string, unknown> = {};
    for (const k of keys) if (source[k] !== undefined) out[k] = source[k];
    return out as Partial<Item>;
}
