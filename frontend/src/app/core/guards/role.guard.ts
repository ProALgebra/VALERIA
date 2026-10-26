import { inject } from '@angular/core';
import { CanMatchFn } from '@angular/router';
import { AuthService } from '../auth/auth.service';

/**
 * Role checks are `canMatch`: when they fail, the router falls through to the next route with the same path,
 * which renders the access-denied view in the content area. The URL stays as typed (SPEC §4).
 */
export const adminMatch: CanMatchFn = async () => (await inject(AuthService).ensureLoaded())?.role === 'admin';

/** Folder work is not available to «Задачи и вопросы» accounts. */
export const notTasksOnlyMatch: CanMatchFn = async () => {
    const auth = inject(AuthService);
    const user = await auth.ensureLoaded();
    return !!user && !auth.tasksOnly();
};
