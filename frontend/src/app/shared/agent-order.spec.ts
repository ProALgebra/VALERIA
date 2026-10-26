import { planFirst } from './agent-order';

describe('planFirst', () => {
    it('puts plan before build and keeps other agents after them in server order', () => {
        expect(planFirst(['build', 'review', 'plan', 'docs'])).toEqual(['plan', 'build', 'review', 'docs']);
    });

    it('does not mutate the input', () => {
        const input = ['build', 'plan'];
        planFirst(input);
        expect(input).toEqual(['build', 'plan']);
    });
});
