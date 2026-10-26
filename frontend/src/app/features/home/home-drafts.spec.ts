import { TestBed } from '@angular/core/testing';
import { HomeDrafts } from './home-drafts';

describe('HomeDrafts', () => {
    let drafts: HomeDrafts;

    beforeEach(() => {
        localStorage.clear();
        sessionStorage.clear();
        drafts = TestBed.inject(HomeDrafts);
    });

    const stored = () => Object.keys(sessionStorage).sort();

    it('keeps the text, the mode and task additions in sessionStorage', () => {
        drafts.setText('почему падает сборка');
        drafts.setMode('do');
        drafts.setAddition('t-ci', 'https://gitlab/pipelines/1');
        expect(stored().length).toBe(3);
        expect(localStorage.length).toBe(0);

        const fresh = TestBed.inject(HomeDrafts);
        expect(fresh.text()).toBe('почему падает сборка');
        expect(fresh.mode()).toBe('do');
        expect(fresh.addition('t-ci')).toBe('https://gitlab/pipelines/1');
        expect(fresh.addition('t-mr')).toBe('');
    });

    it('defaults to «Я только спросить» and removes keys when emptied', () => {
        expect(drafts.mode()).toBe('ask');
        drafts.setMode('do');
        drafts.setText('x');
        drafts.setAddition(5, 'y');
        drafts.setMode('ask');
        drafts.setText('');
        drafts.setAddition(5, '');
        expect(stored()).toEqual([]);
        expect(drafts.mode()).toBe('ask');
    });

    it('prepare() sets mode and text in one go', () => {
        drafts.prepare('ask', 'conan');
        expect(drafts.mode()).toBe('ask');
        expect(drafts.text()).toBe('conan');
    });
});
