import { isWaiting, isWorking } from '../status/status';
import { Item } from './item.model';

export interface ItemGroups {
    /** Waiting for the user or working, newest created first. */
    active: Item[];
    /** Finished chats, newest first. */
    done: Item[];
    /** Snoozed until a future moment, soonest return first. */
    snoozed: Item[];
    /** Archived, most recently archived first. */
    archive: Item[];
}

export function lastActivity(item: Item): number {
    return toTime(item.updated_at) ?? toTime(item.created_at) ?? 0;
}

export function isSnoozed(item: Item, now: number): boolean {
    const until = toTime(item.snoozed_until);
    return until !== null && until > now;
}

/**
 * Splits items into the menu groups (SPEC §6.1). Archive wins over snooze; an expired snooze puts the chat
 * back into Active/Done.
 */
export function groupItems(items: readonly Item[], now: number): ItemGroups {
    const active: Item[] = [];
    const done: Item[] = [];
    const snoozed: Item[] = [];
    const archive: Item[] = [];
    for (const item of items) {
        if (item.archived_at) archive.push(item);
        else if (isSnoozed(item, now)) snoozed.push(item);
        else if (isWaiting(item.status) || isWorking(item.status)) active.push(item);
        else done.push(item);
    }
    const newest = (a: Item, b: Item) => lastActivity(b) - lastActivity(a);
    active.sort((a, b) => (toTime(b.created_at) ?? 0) - (toTime(a.created_at) ?? 0));
    done.sort(newest);
    snoozed.sort((a, b) => (toTime(a.snoozed_until) ?? 0) - (toTime(b.snoozed_until) ?? 0));
    archive.sort((a, b) => (toTime(b.archived_at) ?? 0) - (toTime(a.archived_at) ?? 0));
    return { active, done, snoozed, archive };
}

/** The nearest future snooze return, to schedule a regroup. */
export function nextSnoozeReturn(items: readonly Item[], now: number): number | null {
    let next: number | null = null;
    for (const item of items) {
        if (item.archived_at) continue;
        const until = toTime(item.snoozed_until);
        if (until !== null && until > now && (next === null || until < next)) next = until;
    }
    return next;
}

function toTime(value: string | null | undefined): number | null {
    if (!value) return null;
    const t = Date.parse(value);
    return Number.isNaN(t) ? null : t;
}
