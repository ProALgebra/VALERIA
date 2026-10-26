import { provideHttpClient, withInterceptors } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { ApiError } from './api-error';
import { apiInterceptor, ApiService, CsrfTokenStore } from './api.service';

describe('ApiService', () => {
    let api: ApiService;
    let http: HttpTestingController;

    beforeEach(() => {
        TestBed.configureTestingModule({
            providers: [provideRouter([]), provideHttpClient(withInterceptors([apiInterceptor])), provideHttpClientTesting()],
        });
        api = TestBed.inject(ApiService);
        http = TestBed.inject(HttpTestingController);
    });

    afterEach(() => http.verify());

    it('sends the CSRF token and the idempotency key on writes', async () => {
        TestBed.inject(CsrfTokenStore).set('t-1');
        const done = api.post('/api/chats', { a: 1 }, { idempotencyKey: 'k-1' });
        const req = http.expectOne('/api/chats');
        expect(req.request.headers.get('x-csrf-token')).toBe('t-1');
        expect(req.request.headers.get('Idempotency-Key')).toBe('k-1');
        req.flush({ id: 'c-1' });
        await expect(done).resolves.toEqual({ id: 'c-1' });
    });

    it('normalizes structured errors', async () => {
        const done = api.patch('/api/admin/tasks/t-1', {});
        http.expectOne('/api/admin/tasks/t-1').flush(
            { detail: { code: 'conflict', updated_by: 'admin' } },
            { status: 409, statusText: 'Conflict' },
        );
        const error = await done.catch((e: unknown) => e);
        expect(error).toBeInstanceOf(ApiError);
        expect((error as ApiError).code).toBe('conflict');
        expect((error as ApiError).isConflict).toBe(true);
    });

    it('aborts the request when the signal fires', async () => {
        const controller = new AbortController();
        const failed = api.get('/api/admin/statistics/runs', { signal: controller.signal }).catch((e: unknown) => e);
        const req = http.expectOne('/api/admin/statistics/runs');
        controller.abort();
        expect(req.cancelled).toBe(true);
        const error = await failed;
        expect((error as ApiError).code).toBe('aborted');
    });

    it('rejects at once with an already aborted signal', async () => {
        const controller = new AbortController();
        controller.abort();
        const error = await api.get('/api/me/items', { signal: controller.signal }).catch((e: unknown) => e);
        expect((error as ApiError).code).toBe('aborted');
        http.expectNone('/api/me/items');
    });
});
