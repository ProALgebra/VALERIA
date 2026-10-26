/**
 * Node 25+ ships its own `localStorage` / `sessionStorage` globals, which are unusable without
 * `--localstorage-file` and shadow jsdom's. Point both back at jsdom's Web Storage so specs behave
 * the same under any Node version.
 */
type JsdomGlobal = { jsdom?: { window: { localStorage: Storage; sessionStorage: Storage } } };

for (const name of ['localStorage', 'sessionStorage'] as const) {
    let usable = false;
    try {
        usable = typeof globalThis[name]?.clear === 'function';
    } catch {
        usable = false;
    }
    const jsdomWindow = (globalThis as unknown as JsdomGlobal).jsdom?.window;
    if (!usable && jsdomWindow) {
        Object.defineProperty(globalThis, name, { configurable: true, value: jsdomWindow[name] });
    }
}
