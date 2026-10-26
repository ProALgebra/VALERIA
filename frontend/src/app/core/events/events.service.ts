import { DestroyRef, inject, Injectable, NgZone, signal } from '@angular/core';
import { filter, Observable, Subject } from 'rxjs';

/** Every SSE message is JSON with a `type` field (SPEC §14). */
export interface ServerEvent {
    type: string;
    [key: string]: unknown;
}

export interface ChatUpdatedEvent extends ServerEvent {
    type: 'chat.updated';
    chat_id: string | number;
    kind?: string;
    title?: string;
    archived_at?: string | null;
    snoozed_until?: string | null;
    status?: string;
}

export interface ChatDeletedEvent extends ServerEvent {
    type: 'chat.deleted';
    chat_id: string | number;
}

export interface ConsultationUpdatedEvent extends ServerEvent {
    type: 'consultation.updated';
    job: { id?: string | number; chat_id?: string | number; status?: string; title?: string; [k: string]: unknown };
}

export interface TaskUpdatedEvent extends ServerEvent {
    type: 'task.updated';
    task: { id: string | number; status?: string; title?: string; phase?: string | null; [k: string]: unknown };
}

export interface TaskTitleUpdatedEvent extends ServerEvent {
    type: 'task.title.updated';
    run_id: string | number;
    chat_id?: string | number;
    title: string;
}

export interface KnownEvents {
    'chat.updated': ChatUpdatedEvent;
    'chat.deleted': ChatDeletedEvent;
    'consultation.updated': ConsultationUpdatedEvent;
    'task.updated': TaskUpdatedEvent;
    'task.title.updated': TaskTitleUpdatedEvent;
}

interface Poller {
    fn: () => void;
    ms: number;
    timer: ReturnType<typeof setInterval> | null;
}

/** Reconnect pauses: 1.5 → 3 → 6 … up to 30 s (SPEC §14). */
export const RECONNECT_DELAYS_MS = [1500, 3000, 6000, 12000, 24000, 30000] as const;

export function reconnectDelay(attempt: number): number {
    return RECONNECT_DELAYS_MS[Math.min(attempt, RECONNECT_DELAYS_MS.length - 1)];
}

/**
 * One `EventSource('/api/events')` for the whole app. Screens subscribe with `on(type)`.
 * The backend has no `Last-Event-ID`, so after a reconnect `reconnected$` fires and lists must be re-read.
 */
@Injectable({ providedIn: 'root' })
export class EventsService {
    private readonly zone = inject(NgZone);
    private source: EventSource | null = null;
    private attempt = 0;
    private retryTimer: ReturnType<typeof setTimeout> | null = null;
    private hadConnection = false;
    /** Between `start()` and `stop()`: the polling fallback only runs while signed in. */
    private started = false;
    private readonly pollers = new Set<Poller>();

    readonly connected = signal(false);
    readonly events$ = new Subject<ServerEvent>();
    /** Fires after the stream comes back after a break. */
    readonly reconnected$ = new Subject<void>();

    constructor() {
        inject(DestroyRef).onDestroy(() => this.stop());
    }

    on<K extends keyof KnownEvents>(type: K): Observable<KnownEvents[K]>;
    on<T extends ServerEvent = ServerEvent>(type: string): Observable<T>;
    on(type: string): Observable<ServerEvent> {
        return this.events$.pipe(filter((e) => e.type === type));
    }

    /** Opens the stream (idempotent). Called by the shell after sign-in. */
    start(): void {
        if (!this.started) {
            this.started = true;
            if (!this.connected()) this.pollers.forEach((p) => this.startPoller(p));
        }
        if (this.source || this.retryTimer || typeof EventSource === 'undefined') return;
        this.open();
    }

    /** Closes the stream and stops the polling fallback (sign-out, 401): no requests run after it. */
    stop(): void {
        this.started = false;
        if (this.retryTimer) clearTimeout(this.retryTimer);
        this.retryTimer = null;
        this.source?.close();
        this.source = null;
        this.attempt = 0;
        this.hadConnection = false;
        this.connected.set(false);
        this.pollers.forEach((p) => this.stopPoller(p));
    }

    /**
     * Polling fallback: runs `fn` every `ms` while the service is started and SSE is down (old UI polled questions
     * every 3 s, consultations every 5 s). Returns an unregister function — call it in `DestroyRef.onDestroy`.
     */
    pollWhileDisconnected(fn: () => void, ms: number): () => void {
        const poller = { fn, ms, timer: null as ReturnType<typeof setInterval> | null };
        this.pollers.add(poller);
        if (this.started && !this.connected()) this.startPoller(poller);
        return () => {
            this.stopPoller(poller);
            this.pollers.delete(poller);
        };
    }

    private open(): void {
        this.retryTimer = null;
        const source = new EventSource('/api/events', { withCredentials: true });
        this.source = source;
        source.onopen = () =>
            this.zone.run(() => {
                const wasBroken = this.hadConnection || this.attempt > 0;
                this.attempt = 0;
                this.hadConnection = true;
                this.setConnected(true);
                if (wasBroken) this.reconnected$.next();
            });
        source.onmessage = (message: MessageEvent<string>) => {
            let data: unknown;
            try {
                data = JSON.parse(message.data);
            } catch {
                return;
            }
            if (data && typeof data === 'object' && typeof (data as ServerEvent).type === 'string') {
                this.zone.run(() => this.events$.next(data as ServerEvent));
            }
        };
        source.onerror = () =>
            this.zone.run(() => {
                source.close();
                if (this.source !== source) return;
                this.source = null;
                this.setConnected(false);
                const delay = reconnectDelay(this.attempt++);
                this.retryTimer = setTimeout(() => this.open(), delay);
            });
    }

    private setConnected(value: boolean): void {
        if (this.connected() === value) return;
        this.connected.set(value);
        for (const p of this.pollers) {
            if (value) this.stopPoller(p);
            else if (this.started) this.startPoller(p);
        }
    }

    private startPoller(p: Poller): void {
        if (p.timer) return;
        p.timer = setInterval(() => p.fn(), p.ms);
    }

    private stopPoller(p: Poller): void {
        if (p.timer) clearInterval(p.timer);
        p.timer = null;
    }
}
