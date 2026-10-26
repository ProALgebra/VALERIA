import { buildReplyPayload, emptySelection, QuestionRequest } from './clarify.model';

const single: QuestionRequest = {
    id: 'que_1',
    questions: [
        {
            question: 'В коде два механизма обновления. О каком речь?',
            multiple: false,
            custom: true,
            options: [{ label: 'OTA прошивки устройства' }, { label: 'Обновление конфигурации по MQTT' }],
        },
    ],
};

describe('buildReplyPayload', () => {
    it('is null until something is chosen', () => {
        expect(buildReplyPayload(single, [emptySelection()])).toBeNull();
    });

    it('sends the chosen option as a one-item list', () => {
        expect(buildReplyPayload(single, [{ selected: ['OTA прошивки устройства'], customOn: false, customText: '' }])).toEqual({
            answers: [['OTA прошивки устройства']],
        });
    });

    it('custom answer replaces the option for a single-choice question and is trimmed', () => {
        expect(
            buildReplyPayload(single, [{ selected: ['OTA прошивки устройства'], customOn: true, customText: '  про загрузчик  ' }]),
        ).toEqual({ answers: [['про загрузчик']] });
    });

    it('custom on with empty text is incomplete', () => {
        expect(buildReplyPayload(single, [{ selected: [], customOn: true, customText: '   ' }])).toBeNull();
    });

    it('ignores custom text when the question forbids it', () => {
        const strict: QuestionRequest = { id: 'q', questions: [{ ...single.questions[0], custom: false }] };
        expect(buildReplyPayload(strict, [{ selected: [], customOn: true, customText: 'x' }])).toBeNull();
        expect(buildReplyPayload(strict, [{ selected: ['OTA прошивки устройства'], customOn: true, customText: 'x' }])).toEqual({
            answers: [['OTA прошивки устройства']],
        });
    });

    it('multiple: options in their order plus the custom text, one list per question', () => {
        const req: QuestionRequest = {
            id: 'q',
            questions: [
                { question: 'Какие профили?', multiple: true, custom: true, options: [{ label: 'a' }, { label: 'b' }, { label: 'c' }] },
                { question: 'Ветка?', multiple: false, options: [{ label: 'main' }, { label: 'dev' }] },
            ],
        };
        expect(
            buildReplyPayload(req, [
                { selected: ['c', 'a'], customOn: true, customText: 'd' },
                { selected: ['dev'], customOn: false, customText: '' },
            ]),
        ).toEqual({ answers: [['a', 'c', 'd'], ['dev']] });
    });

    it('drops labels that are not among the options', () => {
        expect(buildReplyPayload(single, [{ selected: ['nope'], customOn: false, customText: '' }])).toBeNull();
    });
});
