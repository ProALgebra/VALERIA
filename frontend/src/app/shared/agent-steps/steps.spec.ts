import { OcPart } from './opencode.model';
import { formatDuration, partsToBlocks, plural, toolPartsToSteps } from './steps';

let n = 0;
const tool = (name: string, state: OcPart['state']): OcPart => ({ id: `p${++n}`, type: 'tool', tool: name, state: { status: 'completed', ...state } });
const read = (filePath: string) => tool('read', { input: { filePath } });
const grep = (pattern: string, matches: number) => tool('grep', { input: { pattern }, metadata: { matches } });
const edit = (file: string, additions: number, deletions: number) =>
    tool('edit', { input: { filePath: file }, metadata: { filediff: { file, additions, deletions } } });

describe('plural', () => {
    it.each([
        [1, 'файл'],
        [2, 'файла'],
        [5, 'файлов'],
        [11, 'файлов'],
        [21, 'файл'],
        [22, 'файла'],
        [112, 'файлов'],
    ])('%i → %s', (count, word) => expect(plural(count, 'файл', 'файла', 'файлов')).toBe(word));
});

describe('formatDuration', () => {
    it.each([
        [134_000, '2 мин 14 с'],
        [45_000, '45 с'],
        [120_000, '2 мин'],
        [3_780_000, '1 ч 3 мин'],
    ])('%i ms → %s', (ms, text) => expect(formatDuration(ms)).toBe(text));
});

describe('toolPartsToSteps', () => {
    it('groups consecutive reads with the first three names and «ещё N»', () => {
        const steps = toolPartsToSteps(['a.py', 'b.txt', 'c/d', 'e', 'f', 'g'].map(read));
        expect(steps).toHaveLength(1);
        expect(steps[0]).toMatchObject({ icon: 'read', text: 'Прочитал 6 файлов', sub: 'a.py, b.txt, c/d, ещё 3' });
    });

    it('names a single read file', () => {
        expect(toolPartsToSteps([read('conanfile.py')])[0].text).toBe('Прочитал conanfile.py');
    });

    it('groups searches and sums matches', () => {
        const [step] = toolPartsToSteps([grep('conans', 6), grep('conan_basic_setup', 3)]);
        expect(step).toMatchObject({ icon: 'search', text: 'Искал в репозитории', sub: '«conans», «conan_basic_setup»: 9 совпадений' });
    });

    it('shows each edit with its diff and collapses edits in one folder', () => {
        const steps = toolPartsToSteps([
            edit('conanfile.py', 48, 31),
            edit('profiles/armv8-gcc12', 7, 5),
            edit('profiles/x86_64-gcc13', 7, 5),
            edit('.gitlab-ci.yml', 6, 4),
        ]);
        expect(steps.map((s) => [s.text, s.sub])).toEqual([
            ['Изменил conanfile.py', '+48 −31'],
            ['Изменил 2 файла в profiles', 'armv8-gcc12, x86_64-gcc13'],
            ['Изменил .gitlab-ci.yml', '+6 −4'],
        ]);
    });

    it('describes a finished command with its duration', () => {
        const [step] = toolPartsToSteps([
            tool('bash', { input: { command: 'conan create .' }, metadata: { exit: 0 }, time: { start: 1000, end: 135_000 } }),
        ]);
        expect(step).toMatchObject({ icon: 'run', text: 'Выполнил conan create .', sub: 'успешно, 2 мин 14 с', failed: false });
    });

    it('marks a failed command with the exit code', () => {
        const [step] = toolPartsToSteps([tool('bash', { input: { command: 'make' }, metadata: { exit: 2 } })]);
        expect(step).toMatchObject({ sub: 'ошибка, код 2', failed: true });
    });

    it('shows a running command without a result', () => {
        const [step] = toolPartsToSteps([tool('bash', { status: 'running', input: { command: 'make' } })]);
        expect(step).toMatchObject({ text: 'Выполняет make', sub: '', running: true });
    });
});

describe('partsToBlocks', () => {
    it('keeps the order of steps and text and skips step markers and synthetic text', () => {
        const blocks = partsToBlocks([
            { id: 's', type: 'step-start' },
            read('a'),
            read('b'),
            { id: 't1', type: 'text', text: 'План' },
            { id: 'syn', type: 'text', text: 'hidden', synthetic: true },
            edit('a', 1, 0),
            { id: 't2', type: 'text', text: 'Готово' },
            { id: 'f', type: 'step-finish' },
        ]);
        expect(blocks.map((b) => (b.kind === 'text' ? b.text : b.steps.map((s) => s.text).join('|')))).toEqual([
            'Прочитал 2 файла',
            'План',
            'Изменил a',
            'Готово',
        ]);
    });
});
