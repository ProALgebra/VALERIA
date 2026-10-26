import { composerState, IdempotentAction, isSendShortcut, sendShortcutHint } from './send-action';

const base = { busy: false, busyReason: 'Агент ещё работает, дождитесь ответа', emptyReason: 'Напишите сообщение', draft: '', sending: false };

describe('composerState', () => {
    it('busy locks everything and explains why, even with a draft', () => {
        expect(composerState({ ...base, busy: true, draft: 'hi' })).toEqual({
            locked: true,
            disabled: true,
            reason: 'Агент ещё работает, дождитесь ответа',
        });
    });

    it('empty or whitespace draft disables the button with the empty reason', () => {
        expect(composerState({ ...base, draft: '   \n' })).toEqual({ locked: false, disabled: true, reason: 'Напишите сообщение' });
    });

    it('sending blocks without a reason', () => {
        expect(composerState({ ...base, draft: 'hi', sending: true })).toEqual({ locked: true, disabled: true, reason: '' });
    });

    it('a draft enables sending', () => {
        expect(composerState({ ...base, draft: 'hi' })).toEqual({ locked: false, disabled: false, reason: '' });
    });
});

describe('IdempotentAction', () => {
    it('reuses the key on retry of the same payload and renews it after success or a new payload', () => {
        const action = new IdempotentAction();
        const first = action.keyFor({ message: 'a' });
        expect(action.keyFor({ message: 'a' })).toBe(first);
        const other = action.keyFor({ message: 'b' });
        expect(other).not.toBe(first);
        action.done();
        expect(action.keyFor({ message: 'b' })).not.toBe(other);
    });
});

describe('send shortcut', () => {
    it('hint depends on the platform', () => {
        expect(sendShortcutHint('MacIntel')).toBe('⌘ Enter');
        expect(sendShortcutHint('Win32')).toBe('Ctrl + Enter');
    });

    it('accepts Ctrl+Enter and ⌘+Enter only', () => {
        expect(isSendShortcut(new KeyboardEvent('keydown', { key: 'Enter', ctrlKey: true }))).toBe(true);
        expect(isSendShortcut(new KeyboardEvent('keydown', { key: 'Enter', metaKey: true }))).toBe(true);
        expect(isSendShortcut(new KeyboardEvent('keydown', { key: 'Enter' }))).toBe(false);
    });
});
