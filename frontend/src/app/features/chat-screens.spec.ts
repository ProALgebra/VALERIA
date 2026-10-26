import { signal, Type } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { KbqModalService } from '@koobiq/components/modal';
import { KbqToastService } from '@koobiq/components/toast';
import { filter, Subject } from 'rxjs';
import { ApiError } from '../core/api/api-error';
import { ApiService } from '../core/api/api.service';
import { AuthService } from '../core/auth/auth.service';
import { EventsService, ServerEvent } from '../core/events/events.service';
import { ItemsStore } from '../core/items/items.store';
import { NotifyService } from '../core/notify/notify.service';
import { HomeDrafts } from './home/home-drafts';
import { TasksCatalog } from '../shared/tasks/tasks-catalog.service';
import { FolderComponent } from './folder/folder.component';
import { QuestionComponent } from './question/question.component';
import { TaskRunComponent } from './task-run/task-run.component';

/** A promise the test resolves by hand, to finish requests out of order. */
function deferred<T>() {
    let resolve!: (v: T) => void;
    let reject!: (e: unknown) => void;
    const promise = new Promise<T>((res, rej) => {
        resolve = res;
        reject = rej;
    });
    return { promise, resolve, reject };
}

const flush = () => new Promise((r) => setTimeout(r, 0));

/** The screens' logic only: templates are replaced so child components need no setup. */
function setup<T>(
    component: Type<T>,
    get: (url: string) => Promise<unknown>,
    items: { chat_id: string; kind: string; id: string }[] = [],
    { realNotify = false } = {},
) {
    const events = new Subject<ServerEvent>();
    const post = vi.fn().mockResolvedValue({});
    const notify = { error: vi.fn(), success: vi.fn(), warning: vi.fn() };
    const toast = { show: vi.fn() };
    const store = {
        byChatId: (id: string) => items.find((i) => i.chat_id === id) ?? null,
        find: (p: (i: unknown) => boolean) => items.find(p) ?? null,
        patchChat: vi.fn(),
    };
    TestBed.configureTestingModule({
        providers: [
            provideRouter([]),
            { provide: ApiService, useValue: { get: vi.fn(get), post } },
            {
                provide: EventsService,
                useValue: {
                    on: (type: string) => events.pipe(filter((e) => e.type === type)),
                    reconnected$: new Subject<void>(),
                    pollWhileDisconnected: () => () => undefined,
                },
            },
            { provide: ItemsStore, useValue: store },
            realNotify ? { provide: KbqToastService, useValue: toast } : { provide: NotifyService, useValue: notify },
            { provide: AuthService, useValue: { user: signal(null) } },
            { provide: KbqModalService, useValue: { create: vi.fn() } },
            { provide: TasksCatalog, useValue: { ensure: () => Promise.resolve(), find: () => null } },
            { provide: HomeDrafts, useValue: {} },
        ],
    });
    TestBed.overrideComponent(component, { set: { template: '', imports: [] } });
    const fixture: ComponentFixture<T> = TestBed.createComponent(component);
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const cmp = fixture.componentInstance as any;
    const open = async (id: string) => {
        fixture.componentRef.setInput('id', id);
        fixture.detectChanges();
        await flush();
    };
    return { fixture, cmp, events, post, notify, toast, open, store };
}

const notFound = () => Promise.reject(new ApiError(404, null, null, null));

describe('question screen', () => {
    const detail = (id: string, status: string) => ({ id, title: id, status, messages: [], created_at: '', updated_at: '' });

    it('switches to «not found» on chat.deleted for the open chat only', async () => {
        const { cmp, events, open } = setup(QuestionComponent, (url) =>
            url.startsWith('/api/consultations/') ? Promise.resolve(detail('q1', 'completed')) : Promise.resolve([]),
        );
        await open('q1');
        events.next({ type: 'chat.deleted', chat_id: 'other' });
        expect(cmp.notFound()).toBe(false);
        events.next({ type: 'chat.deleted', chat_id: 'q1' });
        expect(cmp.notFound()).toBe(true);
    });

    it('a queue response for the previous chat does not overwrite the current one', async () => {
        const queueA = deferred<unknown>();
        const rowB = { id: 'jb', chat_id: 'qb', status: 'failed', error_code: 'x', created_at: '2' };
        let current = 'qa';
        const { cmp, open } = setup(QuestionComponent, (url) => {
            if (url === '/api/consultations/qa') return Promise.resolve(detail('qa', 'queued'));
            if (url === '/api/consultations/qb') return Promise.resolve(detail('qb', 'failed'));
            if (url === '/api/queue/me') return current === 'qa' ? queueA.promise : Promise.resolve([rowB]);
            return Promise.resolve([]);
        });
        await open('qa');
        current = 'qb';
        await open('qb');
        expect(cmp.job()).toEqual(rowB);
        // The older response (taken before B's job existed) arrives last.
        queueA.resolve([{ id: 'ja', chat_id: 'qa', status: 'queued', position: 3, created_at: '1' }]);
        await flush();
        expect(cmp.job()).toEqual(rowB);
    });

    it('drops clarifying questions and their error that arrive after a chat switch', async () => {
        const questionsA = deferred<unknown>();
        const { cmp, notify, open } = setup(QuestionComponent, (url) => {
            if (url === '/api/consultations/qa') return Promise.resolve(detail('qa', 'waiting_question'));
            if (url === '/api/consultations/qa/questions') return questionsA.promise;
            if (url === '/api/consultations/qb') return Promise.resolve(detail('qb', 'completed'));
            return Promise.resolve([]);
        });
        await open('qa');
        await open('qb');
        questionsA.reject(new ApiError(500, null, null, null));
        await flush();
        expect(notify.error).not.toHaveBeenCalled();
        expect(cmp.questions()).toEqual([]);
    });

    it('does not reuse the idempotency key of a failed send in another chat', async () => {
        const { cmp, post, open } = setup(QuestionComponent, (url) =>
            url.startsWith('/api/consultations/') ? Promise.resolve(detail(url.split('/')[3], 'completed')) : Promise.resolve([]),
        );
        await open('qa');
        post.mockRejectedValueOnce(new ApiError(0, null, null, null));
        expect(await cmp.ask('Тот же вопрос')).toBe(false);
        await open('qb');
        expect(await cmp.ask('Тот же вопрос')).toBe(true);
        const [first, second] = post.mock.calls;
        expect(first[0]).toBe('/api/consultations/qa/messages');
        expect(second[0]).toBe('/api/consultations/qb/messages');
        expect(first[1]).toEqual({ message: 'Тот же вопрос' });
        expect(second[2].idempotencyKey).not.toBe(first[2].idempotencyKey);
    });
});

describe('question screen: a send that finishes after a chat switch', () => {
    const detail = (id: string) => ({ id, title: id, status: 'completed', messages: [], created_at: '', updated_at: '' });

    it('does not append the message or the status to the chat opened meanwhile', async () => {
        const { cmp, post, open, store } = setup(QuestionComponent, (url) =>
            url.startsWith('/api/consultations/') ? Promise.resolve(detail(url.split('/')[3])) : Promise.resolve([]),
        );
        await open('qa');
        const sent = deferred<unknown>();
        post.mockReturnValueOnce(sent.promise);
        const result = cmp.ask('Вопрос для A');
        await open('qb');
        store.patchChat.mockClear();
        sent.resolve({});
        expect(await result).toBe(true);
        expect(cmp.detail().id).toBe('qb');
        expect(cmp.detail().messages).toEqual([]);
        expect(cmp.detail().status).toBe('completed');
        expect(store.patchChat).toHaveBeenCalledWith('qa', { status: 'queued' });
        expect(store.patchChat).not.toHaveBeenCalledWith('qb', expect.anything());
    });
});

describe('folder screen', () => {
    const chat = (id: string, status = 'idle') => ({ id, title: id, status, workspace_snapshot: '/w', opencode_session_id: `s-${id}` });

    it('switches to «not found» on chat.deleted for the open chat', async () => {
        const { cmp, events, open } = setup(FolderComponent, (url) =>
            url === '/api/chats/f1' ? Promise.resolve(chat('f1')) : url === '/api/queue/me' ? Promise.resolve([]) : Promise.resolve(url.includes('options') ? {} : []),
        );
        await open('f1');
        expect(cmp.notFound()).toBe(false);
        events.next({ type: 'chat.deleted', chat_id: 'f1' });
        expect(cmp.notFound()).toBe(true);
    });

    it('drops permissions, MCP servers and the queue row of the previous chat', async () => {
        const permsA = deferred<unknown>();
        const mcpA = deferred<unknown>();
        const queueA = deferred<unknown>();
        let current = 'fa';
        const { cmp, open } = setup(FolderComponent, (url) => {
            if (url === '/api/chats/fa') return Promise.resolve(chat('fa'));
            if (url === '/api/chats/fb') return Promise.resolve(chat('fb'));
            if (url === '/api/chats/fa/permissions') return permsA.promise;
            if (url === '/api/chats/fa/mcp') return mcpA.promise;
            if (url === '/api/queue/me') return current === 'fb' ? Promise.resolve([]) : queueA.promise;
            if (url.startsWith('/api/options')) return Promise.resolve({});
            return Promise.resolve([]);
        });
        await open('fa');
        current = 'fb';
        await open('fb');
        permsA.resolve([{ id: 'p1', permission: 'bash', patterns: [] }]);
        mcpA.resolve({ servers: [{ name: 'old' }] });
        queueA.resolve([{ id: 'j1', chat_id: 'fa', status: 'failed', created_at: '1' }]);
        await flush();
        expect(cmp.permissions()).toEqual([]);
        expect(cmp.mcp()).toEqual([]);
        expect(cmp.job()).toBeNull();
    });

    it('a send that finishes after a chat switch leaves the chat opened meanwhile alone', async () => {
        const { cmp, post, open, store } = setup(FolderComponent, (url) => {
            const m = /^\/api\/chats\/([^/]+)$/.exec(url);
            if (m) return Promise.resolve(chat(m[1]));
            return Promise.resolve(url.startsWith('/api/options') ? {} : []);
        });
        await open('fa');
        const sent = deferred<unknown>();
        post.mockReturnValueOnce(sent.promise);
        const result = cmp.send('Запусти тесты');
        await open('fb');
        store.patchChat.mockClear();
        sent.resolve({});
        expect(await result).toBe(true);
        expect(cmp.chat().id).toBe('fb');
        expect(cmp.chat().status).toBe('idle');
        expect(cmp.pendingText()).toBe('');
        expect(store.patchChat).toHaveBeenCalledWith('fa', { status: 'queued' });
        expect(store.patchChat).not.toHaveBeenCalledWith('fb', expect.anything());
    });

    it('does not reuse the idempotency key of a failed send in another chat', async () => {
        const { cmp, post, open } = setup(FolderComponent, (url) => {
            const m = /^\/api\/chats\/([^/]+)$/.exec(url);
            if (m) return Promise.resolve(chat(m[1]));
            return Promise.resolve(url.startsWith('/api/options') ? {} : []);
        });
        await open('fa');
        post.mockRejectedValueOnce(new ApiError(0, null, null, null));
        expect(await cmp.send('Запусти тесты')).toBe(false);
        await open('fb');
        expect(await cmp.send('Запусти тесты')).toBe(true);
        const [first, second] = post.mock.calls;
        expect(first[0]).toBe('/api/chats/fa/messages');
        expect(second[0]).toBe('/api/chats/fb/messages');
        expect(second[1]).toEqual(first[1]);
        expect(second[2].idempotencyKey).not.toBe(first[2].idempotencyKey);
    });
});

describe('task-run screen', () => {
    const run = { id: 'r1', task_id: 't1', plan_first: false, phase: null, title: 'Т', status: 'running', user_input: null, error_code: null, created_at: '', started_at: null, finished_at: null };

    it('switches to «not found» when the chat of the open run is deleted', async () => {
        const items = [{ kind: 'task', id: 'r1', chat_id: 'c-r1' }];
        const { cmp, events, open } = setup(TaskRunComponent, (url) => (url === '/api/task-runs/r1' ? Promise.resolve(run) : notFound()), items);
        await open('r1');
        expect(cmp.run()).not.toBeNull();
        events.next({ type: 'chat.deleted', chat_id: 'c-other' });
        expect(cmp.notFound()).toBe(false);
        items.length = 0; // the sidebar drops the row first
        events.next({ type: 'chat.deleted', chat_id: 'c-r1' });
        expect(cmp.notFound()).toBe(true);
        expect(cmp.run()).toBeNull();
    });

    it('asks the API on chat.deleted when the sidebar never had the run, and shows «not found» once the run is gone', async () => {
        let deleted = false;
        const { cmp, events, open } = setup(TaskRunComponent, (url) => (url === '/api/task-runs/r1' && !deleted ? Promise.resolve(run) : notFound()));
        await open('r1');
        expect(cmp.run()).not.toBeNull();
        events.next({ type: 'chat.deleted', chat_id: 'c-other' });
        await flush();
        expect(cmp.notFound()).toBe(false);
        expect(cmp.run()).not.toBeNull();
        deleted = true;
        events.next({ type: 'chat.deleted', chat_id: 'c-r1' });
        await flush();
        expect(cmp.notFound()).toBe(true);
        expect(cmp.run()).toBeNull();
    });
});

describe('a 403 after which access changed shows no toast (SPEC §16.2)', () => {
    const forbidden = (accessChanged: boolean) => {
        const error = new ApiError(403, null, 'This account can only use tasks and consultations', null);
        error.accessChanged = accessChanged;
        return error;
    };
    const folderGet = (url: string) => {
        const m = /^\/api\/chats\/([^/]+)$/.exec(url);
        if (m) return Promise.resolve({ id: m[1], title: m[1], status: 'idle', workspace_snapshot: '/w', opencode_session_id: 's' });
        return Promise.resolve(url.startsWith('/api/options') ? {} : []);
    };
    const questionGet = (url: string) =>
        url.startsWith('/api/consultations/')
            ? Promise.resolve({ id: 'q1', title: 'q1', status: 'completed', messages: [], created_at: '', updated_at: '' })
            : Promise.resolve([]);

    it('folder: a send rejected that way shows no error toast; a plain 403 still does', async () => {
        const { cmp, post, open, toast } = setup(FolderComponent, folderGet, [], { realNotify: true });
        await open('f1');
        post.mockRejectedValueOnce(forbidden(true));
        expect(await cmp.send('Запусти тесты')).toBe(false);
        expect(toast.show).not.toHaveBeenCalled();
        post.mockRejectedValueOnce(forbidden(false));
        expect(await cmp.send('Запусти тесты')).toBe(false);
        expect(toast.show).toHaveBeenCalledTimes(1);
    });

    it('question: an ask rejected that way shows no error toast; a plain 403 still does', async () => {
        const { cmp, post, open, toast } = setup(QuestionComponent, questionGet, [], { realNotify: true });
        await open('q1');
        post.mockRejectedValueOnce(forbidden(true));
        await cmp.ask('Вопрос');
        expect(toast.show).not.toHaveBeenCalled();
        post.mockRejectedValueOnce(forbidden(false));
        await cmp.ask('Вопрос');
        expect(toast.show).toHaveBeenCalledTimes(1);
    });
});
