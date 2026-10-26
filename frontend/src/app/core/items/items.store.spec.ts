import { TestBed } from '@angular/core/testing';
import { filter, Subject } from 'rxjs';
import { ApiError } from '../api/api-error';
import { EventsService, ServerEvent } from '../events/events.service';
import { Item } from './item.model';
import { groupItems, nextSnoozeReturn } from './items-grouping';
import { ItemsApi } from './items.api';
import { ItemsStore } from './items.store';

const T0 = Date.parse('2026-10-24T12:00:00Z');
const iso = (minutes: number) => new Date(T0 + minutes * 60_000).toISOString();

function item(id: string, status: string, updatedMin: number, extra: Partial<Item> = {}): Item {
    return {
        kind: 'question',
        id,
        chat_id: `c-${id}`,
        title: `Чат ${id}`,
        status,
        phase: null,
        task_id: null,
        created_at: iso(-1000),
        updated_at: iso(updatedMin),
        archived_at: null,
        snoozed_until: null,
        ...extra,
    };
}

describe('groupItems', () => {
    it('puts waiting and working into Active, newest created first; the rest is Done by last activity', () => {
        const created = (minutes: number) => ({ created_at: iso(minutes) });
        const items = [
            item('run-old', 'running', -1, created(-500)),
            item('wait-old', 'waiting_question', -2, created(-400)),
            item('done-old', 'completed', -30),
            item('queued-new', 'queued', -5, created(-50)),
            item('perm-new', 'waiting_permission', -1, created(-100)),
            item('failed-new', 'failed', -2),
            item('cancel', 'cancelling', -20, created(-10)),
            item('dispatch', 'dispatching', -10, created(-300)),
            item('idle', 'idle', -60, { kind: 'folder' }),
            item('cancelled', 'cancelled', -3),
        ];
        const g = groupItems(items, T0);
        expect(g.active.map((i) => i.id)).toEqual(['cancel', 'queued-new', 'perm-new', 'dispatch', 'wait-old', 'run-old']);
        expect(g.done.map((i) => i.id)).toEqual(['failed-new', 'cancelled', 'done-old', 'idle']);
        expect(g.snoozed).toEqual([]);
        expect(g.archive).toEqual([]);
    });

    it('falls back to created_at when updated_at is missing', () => {
        const a = item('a', 'completed', 0, { updated_at: null, created_at: iso(-5) });
        const b = item('b', 'completed', -10);
        expect(groupItems([b, a], T0).done.map((i) => i.id)).toEqual(['a', 'b']);
    });

    it('moves future snoozes to Snoozed (soonest first) and expired ones back', () => {
        const items = [
            item('later', 'completed', -1, { snoozed_until: iso(120) }),
            item('soon', 'running', -1, { snoozed_until: iso(30) }),
            item('expired', 'completed', -1, { snoozed_until: iso(-1) }),
        ];
        const g = groupItems(items, T0);
        expect(g.snoozed.map((i) => i.id)).toEqual(['soon', 'later']);
        expect(g.done.map((i) => i.id)).toEqual(['expired']);
        expect(g.active).toEqual([]);
    });

    it('archive wins over snooze and is ordered by archived_at, newest first', () => {
        const items = [
            item('a1', 'completed', -1, { archived_at: iso(-100) }),
            item('a2', 'running', -1, { archived_at: iso(-10), snoozed_until: iso(60) }),
        ];
        const g = groupItems(items, T0);
        expect(g.archive.map((i) => i.id)).toEqual(['a2', 'a1']);
        expect(g.snoozed).toEqual([]);
        expect(g.active).toEqual([]);
    });

    it('finds the next snooze return, ignoring archived and past ones', () => {
        const items = [
            item('p', 'completed', 0, { snoozed_until: iso(-5) }),
            item('x', 'completed', 0, { snoozed_until: iso(10), archived_at: iso(-1) }),
            item('n', 'completed', 0, { snoozed_until: iso(45) }),
            item('m', 'completed', 0, { snoozed_until: iso(90) }),
        ];
        expect(nextSnoozeReturn(items, T0)).toBe(T0 + 45 * 60_000);
        expect(nextSnoozeReturn([], T0)).toBeNull();
    });
});

describe('ItemsStore', () => {
    let store: ItemsStore;
    let events: Subject<ServerEvent>;
    let api: { list: ReturnType<typeof vi.fn>; archive: ReturnType<typeof vi.fn>; snooze: ReturnType<typeof vi.fn>; remove: ReturnType<typeof vi.fn> };

    beforeEach(() => {
        vi.useFakeTimers();
        vi.setSystemTime(T0);
        events = new Subject<ServerEvent>();
        api = {
            list: vi.fn().mockResolvedValue([]),
            archive: vi.fn().mockResolvedValue(null),
            snooze: vi.fn().mockResolvedValue(null),
            remove: vi.fn().mockResolvedValue(undefined),
        };
        const fakeEvents = {
            events$: events,
            reconnected$: new Subject<void>(),
            on: (type: string) => events.pipe(filter((e) => e.type === type)),
            pollWhileDisconnected: () => () => undefined,
        };
        TestBed.configureTestingModule({
            providers: [
                { provide: ItemsApi, useValue: api },
                { provide: EventsService, useValue: fakeEvents },
            ],
        });
        store = TestBed.inject(ItemsStore);
    });

    afterEach(() => {
        vi.useRealTimers();
    });

    it('returns a snoozed chat to its group when the snooze time comes (browser timer)', async () => {
        api.list.mockResolvedValue([item('s', 'completed', -5, { snoozed_until: iso(30) })]);
        await store.start();
        expect(store.groups().snoozed.map((i) => i.id)).toEqual(['s']);
        expect(store.groups().done).toEqual([]);

        vi.setSystemTime(T0 + 30 * 60_000 + 60);
        vi.advanceTimersByTime(30 * 60_000 + 60);

        expect(store.groups().snoozed).toEqual([]);
        expect(store.groups().done.map((i) => i.id)).toEqual(['s']);
    });

    it('patches status from task.updated and reports a final transition', async () => {
        api.list.mockResolvedValue([item('r1', 'running', -5, { kind: 'task' })]);
        await store.start();
        const seen: string[] = [];
        store.transitions$.subscribe((t) => seen.push(`${t.item.id}:${t.from}->${t.to}`));

        events.next({ type: 'task.updated', task: { id: 'r1', status: 'completed' } });

        expect(store.groups().done.map((i) => i.id)).toEqual(['r1']);
        expect(seen).toEqual(['r1:running->completed']);
    });

    it('falls back to job.updated for folder chats: unfinished job status, otherwise idle', async () => {
        api.list.mockResolvedValue([item('f', 'idle', -5, { kind: 'folder' }), item('q', 'completed', -6)]);
        await store.start();

        events.next({ type: 'job.updated', job: { chat_id: 'c-f', status: 'waiting_permission' } });
        expect(store.items().find((i) => i.id === 'f')?.status).toBe('waiting_permission');

        events.next({ type: 'job.updated', job: { chat_id: 'c-f', status: 'completed' } });
        expect(store.items().find((i) => i.id === 'f')?.status).toBe('idle');

        // Questions keep following consultation.updated / chat.updated only.
        events.next({ type: 'job.updated', job: { chat_id: 'c-q', status: 'running' } });
        expect(store.items().find((i) => i.id === 'q')?.status).toBe('completed');
    });

    it('applies chat.updated / chat.deleted', async () => {
        api.list.mockResolvedValue([item('a', 'completed', -5), item('b', 'completed', -6)]);
        await store.start();

        events.next({ type: 'chat.updated', chat_id: 'c-a', archived_at: iso(0), snoozed_until: null });
        expect(store.groups().archive.map((i) => i.id)).toEqual(['a']);

        events.next({ type: 'chat.deleted', chat_id: 'c-b' });
        expect(store.items().map((i) => i.id)).toEqual(['a']);
    });

    it('updates the list after archive / snooze / delete actions', async () => {
        const a = item('a', 'completed', -5);
        api.list.mockResolvedValue([a, item('b', 'running', -1)]);
        await store.start();

        await store.snooze(a, iso(60));
        expect(api.snooze).toHaveBeenCalledWith('c-a', iso(60));
        expect(store.groups().snoozed.map((i) => i.id)).toEqual(['a']);

        await store.archive(store.byChatId('c-a')!);
        expect(store.groups().archive.map((i) => i.id)).toEqual(['a']);
        expect(store.groups().snoozed).toEqual([]);

        await store.remove(store.byChatId('c-b')!);
        expect(store.items().map((i) => i.id)).toEqual(['a']);
    });

    it('treats 404 on a repeated delete as success and drops the row', async () => {
        api.list.mockResolvedValue([item('a', 'completed', -5), item('b', 'completed', -6)]);
        await store.start();
        api.remove.mockRejectedValueOnce(new ApiError(404, null, 'Chat not found', null));
        await expect(store.remove(store.byChatId('c-b')!)).resolves.toBeUndefined();
        expect(store.items().map((i) => i.id)).toEqual(['a']);
    });

    it('keeps the row and rethrows when delete fails for another reason', async () => {
        api.list.mockResolvedValue([item('a', 'completed', -5)]);
        await store.start();
        const error = new ApiError(500, null, null, null);
        api.remove.mockRejectedValueOnce(error);
        await expect(store.remove(store.byChatId('c-a')!)).rejects.toBe(error);
        expect(store.items().map((i) => i.id)).toEqual(['a']);
    });
});
