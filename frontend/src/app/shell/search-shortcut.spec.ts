import { isSearchShortcut, searchShortcutAria, searchShortcutHint } from './search-shortcut';

const key = (init: KeyboardEventInit) => new KeyboardEvent('keydown', { code: 'KeyK', key: 'k', ...init });

describe('search shortcut', () => {
    it('is Cmd+K on a Mac and Ctrl+K elsewhere', () => {
        expect(isSearchShortcut(key({ metaKey: true }), 'MacIntel')).toBe(true);
        expect(isSearchShortcut(key({ ctrlKey: true }), 'MacIntel')).toBe(false);
        expect(isSearchShortcut(key({ ctrlKey: true }), 'Win32')).toBe(true);
        expect(isSearchShortcut(key({ metaKey: true }), 'Win32')).toBe(false);
    });

    it('matches the physical key, so a Russian layout works too', () => {
        expect(isSearchShortcut(key({ metaKey: true, key: 'л' }), 'MacIntel')).toBe(true);
        expect(isSearchShortcut(key({ metaKey: true, code: 'KeyL', key: 'l' }), 'MacIntel')).toBe(false);
    });

    it('ignores extra modifiers, plain K and IME composition', () => {
        expect(isSearchShortcut(key({ metaKey: true, shiftKey: true }), 'MacIntel')).toBe(false);
        expect(isSearchShortcut(key({ metaKey: true, altKey: true }), 'MacIntel')).toBe(false);
        expect(isSearchShortcut(key({}), 'MacIntel')).toBe(false);
        expect(isSearchShortcut(key({ metaKey: true, isComposing: true }), 'MacIntel')).toBe(false);
    });

    it('describes the shortcut for the menu row and assistive tech', () => {
        expect(searchShortcutHint('MacIntel')).toBe('⌘ K');
        expect(searchShortcutHint('Win32')).toBe('Ctrl + K');
        expect(searchShortcutAria('MacIntel')).toBe('Meta+K');
        expect(searchShortcutAria('Win32')).toBe('Control+K');
    });
});
