import { ConsultationDetail, queueLine, questionTitle } from './question.model';

describe('queueLine', () => {
    it.each([
        [3, 'В очереди, перед вами 2 задания'],
        [2, 'В очереди, перед вами 1 задание'],
        [6, 'В очереди, перед вами 5 заданий'],
        [1, 'В очереди'],
        [null, 'В очереди'],
    ])('position %s → %s', (position, text) => expect(queueLine(position)).toBe(text));
});

describe('questionTitle', () => {
    const base: ConsultationDetail = { id: 'q', title: 'Таймаут OTA', created_at: '', updated_at: '', status: 'queued', messages: [] };
    it('fresh queued or failed question is «Новый вопрос»', () => {
        expect(questionTitle({ ...base, messages: [{ id: '1', role: 'user', text: 'x' }] })).toBe('Новый вопрос');
        expect(questionTitle({ ...base, status: 'failed', messages: [{ id: '1', role: 'user', text: 'x' }] })).toBe('Новый вопрос');
    });
    it('keeps the title for a queued follow-up', () => {
        expect(
            questionTitle({
                ...base,
                messages: [
                    { id: '1', role: 'user', text: 'x' },
                    { id: '2', role: 'assistant', text: 'y' },
                    { id: '3', role: 'user', text: 'z' },
                ],
            }),
        ).toBe('Таймаут OTA');
    });
});
