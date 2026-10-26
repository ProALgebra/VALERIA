import { TestBed } from '@angular/core/testing';
import { ClarifyCardComponent } from './clarify-card.component';
import { QuestionRequest, QuestionSelection } from './clarify.model';

type CardInternals = {
    selections: () => QuestionSelection[];
    pickRadio(i: number, value: string): void;
    setCustomText(i: number, text: string): void;
};

const request = (id: string): QuestionRequest => ({
    id,
    questions: [
        {
            question: 'Для какой платы?',
            header: 'Плата',
            options: [{ label: 'Плата A' }, { label: 'Плата B' }],
            multiple: false,
            custom: true,
        },
        {
            question: 'Комментарий',
            header: 'Комментарий',
            options: [],
            multiple: false,
            custom: true,
        },
    ],
});

describe('ClarifyCardComponent', () => {
    function create() {
        const fixture = TestBed.createComponent(ClarifyCardComponent);
        fixture.componentRef.setInput('request', request('q1'));
        fixture.componentRef.setInput('submit', async () => true);
        fixture.detectChanges();
        return { fixture, card: fixture.componentInstance as unknown as CardInternals };
    }

    it('keeps half-typed answers when the same request is re-fetched', () => {
        const { fixture, card } = create();
        card.pickRadio(0, 'Плата B');
        card.setCustomText(1, 'черновик ответа');

        fixture.componentRef.setInput('request', request('q1')); // new object, same id (re-fetch)
        fixture.detectChanges();

        expect(card.selections()[0].selected).toEqual(['Плата B']);
        expect(card.selections()[1].customText).toBe('черновик ответа');
    });

    it('resets the answers for a new request id', () => {
        const { fixture, card } = create();
        card.pickRadio(0, 'Плата B');

        fixture.componentRef.setInput('request', request('q2'));
        fixture.detectChanges();

        expect(card.selections()[0].selected).toEqual([]);
        expect(card.selections()).toHaveLength(2);
    });
});
