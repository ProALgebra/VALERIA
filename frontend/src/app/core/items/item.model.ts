import { ItemKind } from '../status/status';

/** One chat in the menu, from `GET /api/me/items` (SPEC §13.3). */
export interface Item {
    kind: ItemKind;
    /** Item id in its own domain: consultation id, run id or chat id. */
    id: string | number;
    /** Chat id used by chat actions (`/api/chats/{chat_id}/…`). */
    chat_id: string | number;
    title: string;
    status: string;
    phase: string | null;
    task_id: string | number | null;
    created_at: string;
    updated_at: string | null;
    archived_at: string | null;
    snoozed_until: string | null;
}

/** Route of the chat screen for an item (SPEC §5). */
export function itemRoute(item: Pick<Item, 'kind' | 'id' | 'chat_id'>): string {
    switch (item.kind) {
        case 'question':
            return `/questions/${item.id}`;
        case 'task':
            return `/tasks/runs/${item.id}`;
        case 'folder':
            return `/folder/${item.chat_id}`;
    }
}

/** Stable key for tracking an item across lists. */
export function itemKey(item: Pick<Item, 'kind' | 'id'>): string {
    return `${item.kind}:${item.id}`;
}
