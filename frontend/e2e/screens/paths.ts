import { existsSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';

/** `<shodan>/tmp/new-ui/screens`: the first ancestor of frontend/ that has it (main checkout or a worktree). */
function findScreensRoot(): string {
    let dir = resolve(__dirname, '../..');
    for (let i = 0; i < 6; i++) {
        const candidate = join(dir, 'tmp/new-ui/screens');
        if (existsSync(candidate)) return candidate;
        dir = dirname(dir);
    }
    return resolve(__dirname, '../../../tmp/new-ui/screens');
}

const ROOT = findScreensRoot();
/** Where the prototype shots live: `<group>/proto-<state>[-light|-dark].png`. */
export const PROTO_DIR = process.env['SCREENS_PROTO'] || ROOT;
/** Where live shots, proto copies and INDEX.md go. */
export const OUT_DIR = process.env['SCREENS_OUT'] || join(ROOT, 'final');
/** One JSON per state × theme with the last capture result (kept across partial runs). */
export const RUNS_DIR = join(OUT_DIR, '.runs');
