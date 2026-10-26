import { Component, signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { ApiError } from '../../core/api/api-error';
import { PageStateComponent } from './page-state.component';

@Component({
    imports: [PageStateComponent],
    template: `<v-page-state [error]="error()"><p>content</p></v-page-state>`,
})
class HostComponent {
    readonly error = signal<unknown>(null);
}

describe('PageStateComponent', () => {
    function render(error: unknown): string {
        const fixture = TestBed.createComponent(HostComponent);
        fixture.componentInstance.error.set(error);
        fixture.detectChanges();
        return (fixture.nativeElement as HTMLElement).textContent ?? '';
    }

    it('shows the error state with the backend text for a plain 403', () => {
        const text = render(new ApiError(403, null, 'Administrator role required', null));
        expect(text).toContain('Не удалось загрузить данные');
        expect(text).toContain('Administrator role required');
    });

    it('shows nothing of a 403 after which access changed (SPEC §16.2)', () => {
        const error = new ApiError(403, null, 'Administrator role required', null);
        error.accessChanged = true;
        const text = render(error);
        expect(text).not.toContain('Не удалось загрузить данные');
        expect(text).not.toContain('Administrator role required');
    });
});
