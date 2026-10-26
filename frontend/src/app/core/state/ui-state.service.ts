import { Injectable, Signal, signal, WritableSignal } from '@angular/core';

/**
 * The only place in the app that touches localStorage / sessionStorage (SPEC §6.4).
 *
 * Every value is stored as JSON `{ "v": SCHEMA_VERSION, "value": … }` under a `valeria.`-prefixed key.
 * A read with another schema version, broken JSON or a value that fails validation returns the default.
 */
export const UI_STATE_PREFIX = 'valeria.';
export const UI_STATE_SCHEMA_VERSION = 1;

export interface UiKey<T> {
    /** Key without the `valeria.` prefix, e.g. `nav.snoozed.open`. */
    readonly name: string;
    readonly fallback: T;
    /** Returns the value if it is valid, otherwise `undefined`. */
    readonly parse: (raw: unknown) => T | undefined;
}

export interface UiState<T> extends Signal<T> {
    set(value: T): void;
    update(fn: (value: T) => T): void;
}

export function boolKey(name: string, fallback: boolean): UiKey<boolean> {
    return { name, fallback, parse: (raw) => (typeof raw === 'boolean' ? raw : undefined) };
}

/** Number inside [min, max]; anything else (string, NaN, out of range) falls back to the default. */
export function rangeKey(name: string, fallback: number, min: number, max: number): UiKey<number> {
    return {
        name,
        fallback,
        parse: (raw) => (typeof raw === 'number' && Number.isFinite(raw) && raw >= min && raw <= max ? raw : undefined),
    };
}

export function enumKey<T extends string>(name: string, fallback: T, values: readonly T[]): UiKey<T> {
    return { name, fallback, parse: (raw) => (values.includes(raw as T) ? (raw as T) : undefined) };
}

export const CHAT_ASIDE_WIDTH = { fallback: 320, min: 240, max: 560 } as const;

/** Keys used by the shell. Screens declare their own with the helpers above (`valeria.<screen>.<block>.open`). */
export const UI_KEYS = {
    navSnoozedOpen: boolKey('nav.snoozed.open', false),
    navArchiveOpen: boolKey('nav.archive.open', false),
    chatAsideOpen: boolKey('chat.aside.open', true),
    chatAsideWidth: rangeKey('chat.aside.width', CHAT_ASIDE_WIDTH.fallback, CHAT_ASIDE_WIDTH.min, CHAT_ASIDE_WIDTH.max),
    theme: enumKey('theme', 'system', ['system', 'light', 'dark'] as const),
} as const;

export const CPANEL_WIDTH = { fallback: 480, min: 360, max: 760 } as const;

export function cpanelWidthKey(screen: string): UiKey<number> {
    return rangeKey(`cpanel.${screen}.width`, CPANEL_WIDTH.fallback, CPANEL_WIDTH.min, CPANEL_WIDTH.max);
}

@Injectable({ providedIn: 'root' })
export class UiStateService {
    private readonly cache = new Map<string, WritableSignal<unknown>>();

    read<T>(key: UiKey<T>, storage: Storage | null = this.local()): T {
        if (!storage) return key.fallback;
        let text: string | null = null;
        try {
            text = storage.getItem(UI_STATE_PREFIX + key.name);
        } catch {
            return key.fallback;
        }
        if (text === null) return key.fallback;
        try {
            const box = JSON.parse(text) as { v?: unknown; value?: unknown } | null;
            if (!box || typeof box !== 'object' || box.v !== UI_STATE_SCHEMA_VERSION) return key.fallback;
            const value = key.parse(box.value);
            return value === undefined ? key.fallback : value;
        } catch {
            return key.fallback;
        }
    }

    write<T>(key: UiKey<T>, value: T, storage: Storage | null = this.local()): void {
        if (!storage) return;
        try {
            storage.setItem(UI_STATE_PREFIX + key.name, JSON.stringify({ v: UI_STATE_SCHEMA_VERSION, value }));
        } catch {
            /* quota or private mode: the value just won't survive a reload */
        }
        this.cache.get(key.name)?.set(value);
    }

    remove(key: UiKey<unknown>): void {
        try {
            this.local()?.removeItem(UI_STATE_PREFIX + key.name);
        } catch {
            /* ignore */
        }
        this.cache.get(key.name)?.set(key.fallback);
    }

    /** A signal bound to a localStorage key; `set` persists. Instances are shared per key. */
    state<T>(key: UiKey<T>): UiState<T> {
        let sig = this.cache.get(key.name) as WritableSignal<T> | undefined;
        if (!sig) {
            sig = signal(this.read(key));
            this.cache.set(key.name, sig as WritableSignal<unknown>);
        }
        const s = sig;
        const getter = (() => s()) as UiState<T>;
        getter.set = (value: T) => this.write(key, value);
        getter.update = (fn: (value: T) => T) => this.write(key, fn(s()));
        return getter;
    }

    // ---- Drafts (sessionStorage, SPEC §6.4) ----

    readDraft(id: string): string {
        return this.read(draftKey(id), this.session());
    }

    writeDraft(id: string, text: string): void {
        const storage = this.session();
        if (!text) {
            try {
                storage?.removeItem(UI_STATE_PREFIX + draftKey(id).name);
            } catch {
                /* ignore */
            }
            return;
        }
        this.write(draftKey(id), text, storage);
    }

    clearDraft(id: string): void {
        this.writeDraft(id, '');
    }

    private local(): Storage | null {
        try {
            return typeof localStorage === 'undefined' ? null : localStorage;
        } catch {
            return null;
        }
    }

    private session(): Storage | null {
        try {
            return typeof sessionStorage === 'undefined' ? null : sessionStorage;
        } catch {
            return null;
        }
    }
}

function draftKey(id: string): UiKey<string> {
    return { name: `draft.${id}`, fallback: '', parse: (raw) => (typeof raw === 'string' ? raw : undefined) };
}
