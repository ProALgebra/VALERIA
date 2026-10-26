import { OcMessage } from '../../shared/agent-steps/opencode.model';
import {
    agentLabel,
    alwaysLabel,
    folderPhase,
    folderStateText,
    isFolderBusy,
    upsertMessageInfo,
    upsertPart,
    workingText,
} from './folder.model';

const user = (id: string, agent: string): OcMessage => ({ info: { id, role: 'user', agent }, parts: [] });
const bot = (id: string, agent: string): OcMessage => ({ info: { id, role: 'assistant', agent }, parts: [] });

describe('folderPhase', () => {
    it('maps waiting statuses to cards', () => {
        expect(folderPhase('waiting_permission', [])).toBe('permission');
        expect(folderPhase('waiting_question', [])).toBe('clarify');
    });
    it('treats unfinished statuses as working', () => {
        for (const s of ['queued', 'dispatching', 'running', 'cancelling']) expect(folderPhase(s, [])).toBe('working');
    });
    it('offers Build after a plan answer', () => {
        expect(folderPhase('idle', [user('u', 'plan'), bot('a', 'plan')])).toBe('plan');
        expect(folderPhase('idle', [user('u', 'build'), bot('a', 'build')])).toBe('done');
        expect(folderPhase('idle', [])).toBe('empty');
    });
});

describe('folderStateText', () => {
    it('words the aside state as in the prototype', () => {
        expect(folderStateText('running', [user('u', 'build')], 'В работе')).toBe('Работает');
        expect(folderStateText('running', [user('u', 'plan')], 'В работе')).toBe('Составляет план');
        expect(folderStateText('waiting_permission', [], 'x')).toBe('Ждёт разрешения');
        expect(folderStateText('idle', [user('u', 'plan'), bot('a', 'plan')], 'Готово')).toBe('Ждёт вашего сообщения');
        expect(folderStateText('idle', [user('u', 'build'), bot('a', 'build')], 'x')).toBe('Готово');
    });
    it('keeps the common label for other states', () => {
        expect(folderStateText('queued', [], 'В очереди')).toBe('В очереди');
        expect(folderStateText('failed', [bot('a', 'build')], 'Завершена с ошибкой')).toBe('Завершена с ошибкой');
    });
});

describe('composer busy', () => {
    it('is busy while working or waiting', () => {
        expect(isFolderBusy('running')).toBe(true);
        expect(isFolderBusy('waiting_permission')).toBe(true);
        expect(isFolderBusy('idle')).toBe(false);
    });
});

describe('labels', () => {
    it('capitalises the agent', () => {
        expect(agentLabel('plan')).toBe('Plan');
        expect(agentLabel('')).toBe('');
    });
    it('strips the wildcard from the always pattern', () => {
        expect(alwaysLabel({ id: 'p', always: ['conan create *'] })).toBe('conan create');
        expect(alwaysLabel({ id: 'p', patterns: ['ls'] })).toBe('ls');
    });
    it('says «Составляет план» for the plan agent', () => {
        expect(workingText([user('u', 'plan')])).toBe('Составляет план');
        expect(workingText([user('u', 'build'), bot('a', 'build')])).toBe('Работает');
    });
});

describe('live updates', () => {
    it('upserts message info and parts', () => {
        let list = upsertMessageInfo([], { id: 'a', role: 'assistant', agent: 'build' });
        list = upsertPart(list, { id: 'a-p1', messageID: 'a', type: 'text', text: 'one' });
        list = upsertPart(list, { id: 'a-p1', messageID: 'a', type: 'text', text: 'two' });
        list = upsertPart(list, { id: 'x-p1', messageID: 'x', type: 'text', text: 'lost' });
        expect(list).toHaveLength(1);
        expect(list[0].parts.map((p) => p.text)).toEqual(['two']);
        list = upsertMessageInfo(list, { id: 'a', role: 'assistant', agent: 'build', time: { completed: 1 } });
        expect(list[0].parts).toHaveLength(1);
        expect(list[0].info.time?.completed).toBe(1);
    });
});
