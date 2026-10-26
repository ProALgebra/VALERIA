import { resetPasswordTitle } from './users.component';

describe('resetPasswordTitle', () => {
    it('keeps a plain login as is', () => {
        expect(resetPasswordTitle('p.volkov')).toBe('Сбросить пароль p.volkov?');
    });

    it('escapes markup because the modal title is rendered as HTML', () => {
        expect(resetPasswordTitle('<img src=x onerror=alert(1)>')).toBe(
            'Сбросить пароль &#60;img src=x onerror=alert(1)&#62;?',
        );
    });
});
