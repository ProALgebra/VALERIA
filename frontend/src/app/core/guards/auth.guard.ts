import { inject } from '@angular/core';
import { CanMatchFn, Route, Router, UrlSegment } from '@angular/router';
import { AuthService, User } from '../auth/auth.service';

function attemptedUrl(router: Router, segments: UrlSegment[]): string {
    const nav = router.currentNavigation();
    if (nav) return router.serializeUrl(nav.extractedUrl);
    return '/' + segments.map((s) => s.path).join('/');
}

/** The app shell matches only for a signed-in user; otherwise `/login?return=<url>`. */
export const authMatch: CanMatchFn = async (_route: Route, segments: UrlSegment[]) => {
    const auth = inject(AuthService);
    const router = inject(Router);
    const user = await loadedUser(auth);
    if (user === undefined) return false; // `/api/auth/me` failed: `startupErrorMatch` shows the error state
    if (user) return true;
    const ret = attemptedUrl(router, segments);
    return router.createUrlTree(['/login'], { queryParams: ret && ret !== '/' ? { return: ret } : {} });
};

/** `/login` is for guests only; a signed-in user goes to the `return` URL or home. */
export const guestMatch: CanMatchFn = async () => {
    const auth = inject(AuthService);
    const router = inject(Router);
    const user = await loadedUser(auth);
    if (user === undefined) return false;
    if (!user) return true;
    const nav = router.currentNavigation();
    return router.parseUrl(safeReturnUrl(nav?.extractedUrl.queryParamMap.get('return')));
};

/**
 * Matches any URL while the session check failed with a network error or 5xx (SPEC §7: error state with
 * «Попробовать снова» instead of a blank page). The URL stays as typed so a retry can re-run the navigation.
 */
export const startupErrorMatch: CanMatchFn = () => !!inject(AuthService).loadError();

/** The signed-in user, null for a guest, undefined when `/api/auth/me` failed (network error, 5xx). */
async function loadedUser(auth: AuthService): Promise<User | null | undefined> {
    try {
        return await auth.ensureLoaded();
    } catch {
        return undefined;
    }
}

/** Only same-origin app paths are accepted as a return target (no `//host`, no scheme, no /login loop). */
export function safeReturnUrl(value: string | null | undefined): string {
    if (!value || !value.startsWith('/') || value.startsWith('//') || value.startsWith('/\\')) return '/';
    if (value === '/login' || value.startsWith('/login?') || value.startsWith('/login/')) return '/';
    return value;
}
