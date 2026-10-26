import { inject, Injectable } from '@angular/core';
import { ApiService } from '../api/api.service';
import { Item } from './item.model';

/**
 * The only place that knows the endpoints behind the chat list and chat actions (SPEC §13.3 items 1–2).
 * Integration may swap endpoints here without touching the store or the shell.
 */
@Injectable({ providedIn: 'root' })
export class ItemsApi {
    private readonly api = inject(ApiService);

    async list(): Promise<Item[]> {
        const res = await this.api.get<Item[] | { items: Item[] }>('/api/me/items');
        return Array.isArray(res) ? res : (res?.items ?? []);
    }

    archive(chatId: string | number): Promise<Item | null> {
        return this.api.post<Item | null>(`/api/chats/${enc(chatId)}/archive`);
    }

    unarchive(chatId: string | number): Promise<Item | null> {
        return this.api.post<Item | null>(`/api/chats/${enc(chatId)}/unarchive`);
    }

    /** `until` is ISO 8601 with offset, e.g. `2026-10-26T10:00:00+03:00`. */
    snooze(chatId: string | number, until: string): Promise<Item | null> {
        return this.api.post<Item | null>(`/api/chats/${enc(chatId)}/snooze`, { until });
    }

    unsnooze(chatId: string | number): Promise<Item | null> {
        return this.api.post<Item | null>(`/api/chats/${enc(chatId)}/unsnooze`);
    }

    remove(chatId: string | number): Promise<void> {
        return this.api.delete<void>(`/api/chats/${enc(chatId)}`);
    }
}

function enc(id: string | number): string {
    return encodeURIComponent(String(id));
}
