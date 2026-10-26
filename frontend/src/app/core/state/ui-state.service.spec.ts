import { TestBed } from '@angular/core/testing';
import { cpanelWidthKey, UI_KEYS, UI_STATE_SCHEMA_VERSION, UiStateService } from './ui-state.service';

describe('UiStateService', () => {
    let svc: UiStateService;
    const put = (key: string, raw: string) => localStorage.setItem(`valeria.${key}`, raw);
    const box = (value: unknown, v = UI_STATE_SCHEMA_VERSION) => JSON.stringify({ v, value });

    beforeEach(() => {
        localStorage.clear();
        sessionStorage.clear();
        svc = TestBed.inject(UiStateService);
    });

    it('returns defaults when nothing is stored', () => {
        expect(svc.read(UI_KEYS.navSnoozedOpen)).toBe(false);
        expect(svc.read(UI_KEYS.navArchiveOpen)).toBe(false);
        expect(svc.read(UI_KEYS.chatAsideOpen)).toBe(true);
        expect(svc.read(UI_KEYS.chatAsideWidth)).toBe(320);
        expect(svc.read(UI_KEYS.theme)).toBe('system');
        expect(svc.read(cpanelWidthKey('users'))).toBe(480);
    });

    it('round-trips values with the prefix and schema version', () => {
        svc.write(UI_KEYS.chatAsideWidth, 400);
        expect(localStorage.getItem('valeria.chat.aside.width')).toBe(box(400));
        expect(svc.read(UI_KEYS.chatAsideWidth)).toBe(400);
    });

    it('rejects out-of-range, non-number and NaN widths', () => {
        put('chat.aside.width', box(239));
        expect(svc.read(UI_KEYS.chatAsideWidth)).toBe(320);
        put('chat.aside.width', box(561));
        expect(svc.read(UI_KEYS.chatAsideWidth)).toBe(320);
        put('chat.aside.width', box('400'));
        expect(svc.read(UI_KEYS.chatAsideWidth)).toBe(320);
        put('chat.aside.width', '{"v":1,"value":NaN}');
        expect(svc.read(UI_KEYS.chatAsideWidth)).toBe(320);
        put('cpanel.audit.width', box(800));
        expect(svc.read(cpanelWidthKey('audit'))).toBe(480);
        put('cpanel.audit.width', box(760));
        expect(svc.read(cpanelWidthKey('audit'))).toBe(760);
    });

    it('rejects broken JSON, raw values and another schema version', () => {
        put('nav.archive.open', 'true');
        expect(svc.read(UI_KEYS.navArchiveOpen)).toBe(false);
        put('nav.archive.open', '{oops');
        expect(svc.read(UI_KEYS.navArchiveOpen)).toBe(false);
        put('nav.archive.open', box(true, UI_STATE_SCHEMA_VERSION + 1));
        expect(svc.read(UI_KEYS.navArchiveOpen)).toBe(false);
        put('nav.archive.open', box('yes'));
        expect(svc.read(UI_KEYS.navArchiveOpen)).toBe(false);
        put('nav.archive.open', box(true));
        expect(svc.read(UI_KEYS.navArchiveOpen)).toBe(true);
    });

    it('accepts only known themes', () => {
        put('theme', box('dark'));
        expect(svc.read(UI_KEYS.theme)).toBe('dark');
        put('theme', box('sepia'));
        expect(svc.read(UI_KEYS.theme)).toBe('system');
    });

    it('state() signals are shared per key and persist on set', () => {
        const a = svc.state(UI_KEYS.navSnoozedOpen);
        const b = svc.state(UI_KEYS.navSnoozedOpen);
        expect(a()).toBe(false);
        a.set(true);
        expect(b()).toBe(true);
        expect(localStorage.getItem('valeria.nav.snoozed.open')).toBe(box(true));
        b.update((v) => !v);
        expect(a()).toBe(false);
    });

    it('keeps drafts in sessionStorage only', () => {
        svc.writeDraft('new-request', 'Почини сборку');
        expect(sessionStorage.getItem('valeria.draft.new-request')).toBe(box('Почини сборку'));
        expect(localStorage.length).toBe(0);
        expect(svc.readDraft('new-request')).toBe('Почини сборку');
        svc.clearDraft('new-request');
        expect(sessionStorage.getItem('valeria.draft.new-request')).toBeNull();
        expect(svc.readDraft('new-request')).toBe('');
    });
});
