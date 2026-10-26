import { assignTaskShapes, TASK_SHAPES } from './shape.component';

describe('TASK_SHAPES', () => {
    it('gives at least 10 task types distinct shapes, none of them «?»', () => {
        expect(TASK_SHAPES.length).toBeGreaterThanOrEqual(10);
        expect(new Set(TASK_SHAPES).size).toBe(TASK_SHAPES.length);
        expect(TASK_SHAPES).not.toContain('question');
        expect(TASK_SHAPES.slice(0, 5)).toEqual(['triangle', 'square', 'diamond', 'down', 'circle']);
    });
});

describe('assignTaskShapes', () => {
    const tasks = [
        { id: 't-yt', title: 'Ответ на упоминание @Valeria в YouTrack' },
        { id: 't-conan', title: 'Обновление зависимости Conan во всех пакетах' },
        { id: 't-dup', title: 'Поиск дубликатов задачи в YouTrack' },
        { id: 't-notes', title: 'Release notes по задачам версии' },
        { id: 't-mr', title: 'Ревью merge request' },
        { id: 't-ci', title: 'Разбор упавшего CI-пайплайна' },
    ];

    it('maps the prototype kinds to their shapes and gives the rest a free one', () => {
        expect(assignTaskShapes(tasks)).toEqual({
            't-ci': 'triangle',
            't-mr': 'square',
            't-notes': 'diamond',
            't-dup': 'down',
            't-yt': 'circle',
            't-conan': 'pentagon',
        });
    });

    it('does not depend on the input order', () => {
        expect(assignTaskShapes([...tasks].reverse())).toEqual(assignTaskShapes(tasks));
    });
});
