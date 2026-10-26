import { legacyHashTarget } from './legacy-hash';
import { safeReturnUrl } from './auth.guard';

describe('legacy hash links', () => {
    it('maps old hash routes to the new paths', () => {
        expect(legacyHashTarget('#/consultations/42')).toBe('/questions/42');
        expect(legacyHashTarget('#/tasks/runs/run-7')).toBe('/tasks/runs/run-7');
        expect(legacyHashTarget('#/tasks/runs/run-7/')).toBe('/tasks/runs/run-7');
        expect(legacyHashTarget('#/consultations/42?x=1')).toBe('/questions/42');
        expect(legacyHashTarget('#/admin')).toBeNull();
        expect(legacyHashTarget('#/tasks/runs')).toBeNull();
        expect(legacyHashTarget('#/consultations/a/b')).toBeNull();
    });

    it('maps old sections to the new screens', () => {
        expect(legacyHashTarget('#/')).toBe('/');
        expect(legacyHashTarget('#/dialogs')).toBe('/');
        expect(legacyHashTarget('#/consultations')).toBe('/');
        expect(legacyHashTarget('#/templates')).toBe('/');
        expect(legacyHashTarget('#/tasks')).toBe('/');
        expect(legacyHashTarget('#/profile')).toBe('/');
        expect(legacyHashTarget('#/statistics')).toBe('/admin/statistics');
        expect(legacyHashTarget('#/admin-consultations')).toBe('/admin/requests');
        expect(legacyHashTarget('#/admin-consultations/c-1')).toBe('/admin/requests');
        expect(legacyHashTarget('#/admin-tasks')).toBe('/admin/tasks');
        expect(legacyHashTarget('#/task-access')).toBe('/admin/tasks');
        expect(legacyHashTarget('#/users')).toBe('/admin/users');
        expect(legacyHashTarget('#/queue')).toBe('/admin/queue');
        expect(legacyHashTarget('#/audit')).toBe('/admin/audit');
        expect(legacyHashTarget('#section')).toBeNull();
        expect(legacyHashTarget('')).toBeNull();
        expect(legacyHashTarget('#/consultations/%E0')).toBeNull();
    });

    it('ignores Object.prototype keys', () => {
        expect(legacyHashTarget('#/constructor')).toBeNull();
        expect(legacyHashTarget('#/__proto__')).toBeNull();
        expect(legacyHashTarget('#/toString')).toBeNull();
        expect(legacyHashTarget('#/hasOwnProperty')).toBeNull();
    });
});

describe('safeReturnUrl', () => {
    it('accepts only in-app paths', () => {
        expect(safeReturnUrl('/questions/1')).toBe('/questions/1');
        expect(safeReturnUrl('/admin/requests?user=a')).toBe('/admin/requests?user=a');
        expect(safeReturnUrl('//evil.example')).toBe('/');
        expect(safeReturnUrl('https://evil.example')).toBe('/');
        expect(safeReturnUrl('/login?return=/x')).toBe('/');
        expect(safeReturnUrl(null)).toBe('/');
    });
});
