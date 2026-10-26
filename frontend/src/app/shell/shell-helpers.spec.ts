import { effectivePanelWidth, panelMaxWidth } from './aside/panel-width';
import { chatMenuMode } from './chat-menu/chat-actions.service';
import { initialsOf } from './nav/nav.component';

describe('chatMenuMode', () => {
    const now = Date.parse('2026-10-24T12:00:00Z');
    it('archived wins, then a future snooze, else normal', () => {
        expect(chatMenuMode({ archived_at: '2026-10-20T10:00:00Z', snoozed_until: '2026-10-25T10:00:00Z' }, now)).toBe('archived');
        expect(chatMenuMode({ archived_at: null, snoozed_until: '2026-10-25T10:00:00Z' }, now)).toBe('snoozed');
        expect(chatMenuMode({ archived_at: null, snoozed_until: '2026-10-24T11:00:00Z' }, now)).toBe('normal');
        expect(chatMenuMode({ archived_at: null, snoozed_until: null }, now)).toBe('normal');
    });
});

describe('effectivePanelWidth', () => {
    it('squeezes to the minimum when the window is narrower than nav + panel + 480', () => {
        expect(effectivePanelWidth(400, 240, 240 + 400 + 480)).toBe(400);
        expect(effectivePanelWidth(400, 240, 240 + 400 + 479)).toBe(240);
    });
});

describe('panelMaxWidth', () => {
    it('stops a drag where the content would get narrower than 480, never below the minimum', () => {
        expect(panelMaxWidth(360, 760, 1920)).toBe(760);
        expect(panelMaxWidth(360, 760, 1440)).toBe(1440 - 240 - 480);
        expect(panelMaxWidth(360, 760, 900)).toBe(360);
        // A width saved at the drag limit is never squeezed in the same window.
        expect(effectivePanelWidth(panelMaxWidth(360, 760, 1440), 360, 1440)).toBe(720);
    });
});

describe('initialsOf', () => {
    it('takes two letters from the login', () => {
        expect(initialsOf('i.sokolova')).toBe('IS');
        expect(initialsOf('admin')).toBe('AD');
        expect(initialsOf('')).toBe('');
    });
});
