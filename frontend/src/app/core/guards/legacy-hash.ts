/** Sections of the previous UI that map to a new screen without an id. */
const SECTIONS: Record<string, string> = {
    '': '/',
    dialogs: '/',
    consultations: '/',
    templates: '/',
    tasks: '/',
    profile: '/',
    statistics: '/admin/statistics',
    'admin-consultations': '/admin/requests',
    'admin-tasks': '/admin/tasks',
    'task-access': '/admin/tasks',
    users: '/admin/users',
    queue: '/admin/queue',
    audit: '/admin/audit',
};

/**
 * Old links (Mattermost notifications, bookmarks) point to hash routes of the previous UI.
 * Returns the new path for a known hash, or null. Notification links keep their chat or run id;
 * an old admin consultation id is a chat id, which the requests screen does not address, so it opens the list.
 */
export function legacyHashTarget(hash: string): string | null {
    const m = /^#\/([^?#]*?)\/?(\?.*)?$/.exec(hash);
    if (!m) return null;
    const parts = m[1].split('/');
    if (parts.length === 1) return Object.hasOwn(SECTIONS, parts[0]) ? SECTIONS[parts[0]] : null;
    let id: string;
    try {
        id = encodeURIComponent(decodeURIComponent(parts[parts.length - 1]));
    } catch {
        return null;
    }
    if (parts.length === 2 && parts[0] === 'consultations') return `/questions/${id}`;
    if (parts.length === 3 && parts[0] === 'tasks' && parts[1] === 'runs') return `/tasks/runs/${id}`;
    if (parts.length === 2 && parts[0] === 'admin-consultations') return '/admin/requests';
    return null;
}

/** Rewrites the address before the router starts, so the first navigation already uses the new route. */
export function rewriteLegacyHash(loc: Location = window.location, hist: History = window.history): void {
    const target = legacyHashTarget(loc.hash);
    if (target) hist.replaceState(hist.state, '', target);
}
