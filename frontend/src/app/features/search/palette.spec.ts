import { Item } from '../../core/items/item.model';
import { TaskDef } from '../../shared/tasks/task.model';
import { buildPalette, paletteCommands, RECENT_LIMIT, rowTime } from './palette';

const NOW = Date.parse('2026-10-24T12:00:00Z');

const item = (over: Partial<Item>): Item => ({
    kind: 'question',
    id: 'q-1',
    chat_id: 'q-1',
    title: 'Вопрос',
    status: 'completed',
    phase: null,
    task_id: null,
    created_at: '2026-10-24T08:00:00Z',
    updated_at: null,
    archived_at: null,
    snoozed_until: null,
    ...over,
});

const task = (id: string, title: string, description = ''): TaskDef => ({
    id,
    title,
    description,
    plan_first: false,
    input_hint: null,
});

const ITEMS: Item[] = [
    item({ id: 'q-ota', title: 'Где в коде обрабатывается таймаут OTA?', status: 'waiting_question', updated_at: '2026-10-24T11:00:00Z' }),
    item({ kind: 'folder', id: 'f-conan', title: 'Миграция net-utils на Conan 2', status: 'running', updated_at: '2026-10-24T10:00:00Z' }),
    item({ kind: 'folder', id: 'f-arch', title: 'Профили Conan для тулчейнов', archived_at: '2026-10-20T10:00:00Z', updated_at: '2026-10-20T10:00:00Z' }),
    item({ kind: 'task', id: 'r-snz', title: 'Château Conan snoozed', snoozed_until: '2026-10-30T10:00:00Z', updated_at: '2026-10-23T10:00:00Z' }),
    item({ id: 'q-a', title: 'A', updated_at: '2026-10-24T09:00:00Z' }),
    item({ id: 'q-b', title: 'B', updated_at: '2026-10-24T08:30:00Z' }),
    item({ id: 'q-c', title: 'C', updated_at: '2026-10-24T08:10:00Z' }),
];
const TASKS = [
    task('t-ci', 'Разбор упавшего CI-пайплайна', 'Найдёт причину падения'),
    task('t-conan', 'Обновление зависимости Conan во всех пакетах'),
];

const palette = (query: string, tasksOnly = false) =>
    buildPalette({ query, items: ITEMS, tasks: TASKS, tasksOnly, workspace: 'fw/net-utils', now: NOW });

describe('paletteCommands', () => {
    it('offers «Спросить» and «Сделать» with the folder', () => {
        const cmds = paletteCommands(false, 'fw/net-utils');
        expect(cmds.map((c) => c.title)).toEqual(['Спросить', 'Сделать']);
        expect(cmds[1].sub).toBe('Новая работа в папке fw/net-utils');
    });

    it('has no «Сделать» for tasks-only accounts', () => {
        expect(paletteCommands(true, 'fw/net-utils').map((c) => c.id)).toEqual(['ask']);
    });
});

describe('buildPalette', () => {
    it('without a query shows all commands, all tasks and recent active+done chats without archive or snoozed', () => {
        const res = palette('   ');
        expect(res.commands.length).toBe(2);
        expect(res.tasks.length).toBe(2);
        expect(res.chatsCaption).toBe('Недавние');
        expect(res.chats.length).toBe(RECENT_LIMIT);
        expect(res.chats.map((c) => c.id)).not.toContain('f-arch');
        expect(res.chats.map((c) => c.id)).not.toContain('r-snz');
        expect(res.chats.slice(0, 2).map((c) => c.id)).toEqual(['q-ota', 'f-conan']);
        expect(res.nothing).toBe(false);
    });

    it('«conan» finds tasks and chats including archived and snoozed, newest first', () => {
        const res = palette('conan');
        expect(res.commands).toEqual([]);
        expect(res.tasks.map((t) => t.id)).toEqual(['t-conan']);
        expect(res.chatsCaption).toBe('Чаты');
        expect(res.chats.map((c) => c.id)).toEqual(['f-conan', 'r-snz', 'f-arch']);
    });

    it('is case-insensitive, accent-insensitive and ANDs tokens; quotes keep a phrase', () => {
        expect(palette('CHATEAU').chats.map((c) => c.id)).toEqual(['r-snz']);
        expect(palette('миграция conan').chats.map((c) => c.id)).toEqual(['f-conan']);
        expect(palette('"conan 2"').chats.map((c) => c.id)).toEqual(['f-conan']);
        expect(palette('"2 conan"').nothing).toBe(true);
    });

    it('matches the kind label and the command keywords', () => {
        expect(palette('работа в папке').chats.map((c) => c.id)).toEqual(['f-conan', 'f-arch']);
        expect(palette('новый вопрос').commands.map((c) => c.id)).toEqual(['ask']);
    });

    it('reports «nothing» when no group matched', () => {
        const res = palette('zigbee');
        expect(res.nothing).toBe(true);
        expect(res.chats).toEqual([]);
    });

    it('does not find «Сделать» for tasks-only accounts', () => {
        expect(palette('сделать', true).commands).toEqual([]);
    });
});

describe('rowTime', () => {
    it('shows time today, «Вчера» and a date before', () => {
        const local = new Date(NOW);
        const today = new Date(local.getFullYear(), local.getMonth(), local.getDate(), 9, 5).toISOString();
        const yesterday = new Date(local.getFullYear(), local.getMonth(), local.getDate() - 1, 9, 5).toISOString();
        expect(rowTime(today, NOW)).toBe('09:05');
        expect(rowTime(yesterday, NOW)).toBe('Вчера');
        expect(rowTime('2026-10-12T10:00:00', NOW)).toBe('12 окт');
        expect(rowTime(null, NOW)).toBe('');
    });
});
