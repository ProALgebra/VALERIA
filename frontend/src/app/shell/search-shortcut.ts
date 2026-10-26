const currentPlatform = (): string => (typeof navigator === 'undefined' ? '' : navigator.platform || navigator.userAgent);

const isMac = (platform: string): boolean => /Mac|iPhone|iPad|iPod/i.test(platform);

/** Cmd+K on a Mac, Ctrl+K elsewhere: opens «Поиск и команды» from any screen, even while typing in a field. */
export function isSearchShortcut(event: KeyboardEvent, platform = currentPlatform()): boolean {
    if (event.isComposing || event.shiftKey || event.altKey) return false;
    // `code`, not `key`: the same physical key with a Russian layout gives «л».
    if (event.code !== 'KeyK') return false;
    return isMac(platform) ? event.metaKey && !event.ctrlKey : event.ctrlKey && !event.metaKey;
}

/** The shortcut as shown in the menu row, written like the send hint (`⌘ Enter` / `Ctrl + Enter`). */
export function searchShortcutHint(platform = currentPlatform()): string {
    return isMac(platform) ? '⌘ K' : 'Ctrl + K';
}

/** The same shortcut for `aria-keyshortcuts`. */
export function searchShortcutAria(platform = currentPlatform()): string {
    return isMac(platform) ? 'Meta+K' : 'Control+K';
}
