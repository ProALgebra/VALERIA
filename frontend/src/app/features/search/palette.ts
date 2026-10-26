import { Item } from '../../core/items/item.model';
import { groupItems, lastActivity } from '../../core/items/items-grouping';
import { matches, tokenize } from '../../core/search/search';
import { ItemKind, KIND_ICON } from '../../core/status/status';
import { shortDate, TaskDef } from '../../shared/tasks/task.model';

export type PaletteCommandId = 'ask' | 'do';

export interface PaletteCommand {
    id: PaletteCommandId;
    icon: string;
    title: string;
    sub: string;
    /** Extra words the command is found by. */
    keys: string;
}

export const KIND_LABEL: Record<ItemKind, string> = {
    question: 'Вопрос',
    task: 'Задача',
    folder: 'Работа в папке',
};

/** «Создать» commands (prototype CMDS). «Сделать» is for full accounts only. */
export function paletteCommands(tasksOnly: boolean, workspace: string | null | undefined): PaletteCommand[] {
    const ask: PaletteCommand = {
        id: 'ask',
        icon: KIND_ICON.question,
        title: 'Спросить',
        sub: 'Новый вопрос. Valeria только читает',
        keys: 'новый вопрос',
    };
    if (tasksOnly) return [ask];
    return [
        ask,
        {
            id: 'do',
            icon: KIND_ICON.folder,
            title: 'Сделать',
            sub: `Новая работа в папке ${workspace ?? ''}`.trim(),
            keys: 'новая работа',
        },
    ];
}

export interface PaletteInput {
    query: string;
    items: readonly Item[];
    tasks: readonly TaskDef[];
    tasksOnly: boolean;
    workspace: string | null | undefined;
    now: number;
}

export interface PaletteResult {
    commands: PaletteCommand[];
    tasks: TaskDef[];
    /** «Чаты» when there is a query, «Недавние» without one. */
    chatsCaption: 'Чаты' | 'Недавние';
    chats: Item[];
    /** A query that matched nothing: «Ничего не нашлось». */
    nothing: boolean;
}

export const RECENT_LIMIT = 4;

/**
 * Groups and filters the palette (SPEC §8.5, §12). Searches only what is already loaded: the menu items
 * (archived and snoozed included) and the tasks. Without a query: all commands, all tasks and the 4 most recent
 * chats from the menu (active first, then done; no archive, no snoozed).
 */
export function buildPalette(input: PaletteInput): PaletteResult {
    const tokens = tokenize(input.query);
    const commands = paletteCommands(input.tasksOnly, input.workspace).filter((c) =>
        matches([c.title, c.sub, c.keys], tokens),
    );
    const tasks = input.tasks.filter((t) => matches([t.title, t.description], tokens));
    if (!tokens.length) {
        const groups = groupItems(input.items, input.now);
        const chats = [...groups.active, ...groups.done].slice(0, RECENT_LIMIT);
        return { commands, tasks, chatsCaption: 'Недавние', chats, nothing: false };
    }
    const chats = input.items
        .filter((item) => matches([item.title, KIND_LABEL[item.kind]], tokens))
        .sort((a, b) => lastActivity(b) - lastActivity(a));
    return {
        commands,
        tasks,
        chatsCaption: 'Чаты',
        chats,
        nothing: !commands.length && !tasks.length && !chats.length,
    };
}

/** Time in a chat row: `HH:mm` today, «Вчера», otherwise `12 окт`. */
export function rowTime(iso: string | null | undefined, now: number): string {
    if (!iso) return '';
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return '';
    const today = new Date(now);
    const startOfDay = (x: Date) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
    const days = Math.round((startOfDay(today) - startOfDay(d)) / 86_400_000);
    if (days <= 0) return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
    if (days === 1) return 'Вчера';
    return shortDate(d);
}
