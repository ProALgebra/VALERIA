import { Component } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { provideRouter, Router, RouterOutlet } from '@angular/router';
import { RouterTestingHarness } from '@angular/router/testing';
import { StartupErrorComponent } from '../../shared/startup-error/startup-error.component';
import { ApiError } from '../api/api-error';
import { ApiService } from '../api/api.service';
import { authMatch, guestMatch, startupErrorMatch } from './auth.guard';

@Component({ template: 'login-page' })
class LoginStub {}

@Component({ template: 'shell-page<router-outlet />', imports: [RouterOutlet] })
class ShellStub {}

const ME = { user: { id: 1, login: 'u', role: 'user', tasks_only: false, workspace_path: null }, csrf_token: 't' };

describe('auth guards: /api/auth/me failure at startup', () => {
    let get: ReturnType<typeof vi.fn>;

    beforeEach(() => {
        get = vi.fn();
        TestBed.configureTestingModule({
            providers: [
                { provide: ApiService, useValue: { get, post: vi.fn() } },
                provideRouter([
                    { path: 'login', canMatch: [guestMatch], component: LoginStub },
                    { path: '', canMatch: [authMatch], component: ShellStub, children: [{ path: '**', children: [] }] },
                    { path: '**', canMatch: [startupErrorMatch], component: StartupErrorComponent },
                ]),
            ],
        });
    });

    it('shows the error state instead of a blank page on 502, retry recovers to the opened URL', async () => {
        get.mockRejectedValueOnce(new ApiError(502, null, null, null)).mockResolvedValueOnce(ME);
        const harness = await RouterTestingHarness.create('/questions/7');
        const el = harness.routeNativeElement as HTMLElement;

        expect(el.querySelector('.v-page-state__error')).not.toBeNull();
        expect(el.textContent).toContain('Не удалось загрузить данные');
        expect(TestBed.inject(Router).url).toBe('/questions/7');

        (el.querySelector('.v-page-state__error button') as HTMLButtonElement).click();
        await harness.fixture.whenStable();
        harness.detectChanges();

        expect(get).toHaveBeenCalledTimes(2);
        expect(TestBed.inject(Router).url).toBe('/questions/7');
        expect((harness.routeNativeElement as HTMLElement).textContent).toContain('shell-page');
    });

    it('a network error shows «Сервер недоступен»; a retry that still fails keeps the error state', async () => {
        get.mockRejectedValue(new ApiError(0, null, null, null));
        const harness = await RouterTestingHarness.create('/');
        const el = harness.routeNativeElement as HTMLElement;
        expect(el.textContent).toContain('Сервер недоступен');

        (el.querySelector('.v-page-state__error button') as HTMLButtonElement).click();
        await harness.fixture.whenStable();
        harness.detectChanges();
        expect(get).toHaveBeenCalledTimes(2);
        expect((harness.routeNativeElement as HTMLElement).querySelector('.v-page-state__error')).not.toBeNull();
    });

    it('401 still goes to the login screen', async () => {
        get.mockRejectedValue(new ApiError(401, null, null, null));
        const harness = await RouterTestingHarness.create('/questions/7');
        expect(TestBed.inject(Router).url).toBe('/login?return=%2Fquestions%2F7');
        expect((harness.routeNativeElement as HTMLElement).textContent).toContain('login-page');
    });
});
