import { KbqPipe, KbqPipeTypes } from '@koobiq/components/filter-bar';

/** Id of the search pipe in `onChangePipe`. */
export const ADMIN_SEARCH_PIPE_ID = 'search';

/**
 * The stock filter-bar search: a text pipe that goes first in `kbq-filter-bar` (SPEC §12). The pipe name is its
 * placeholder and accessible name. It commits the typed text (debounced) or `null` after the cleaner; the screen
 * filters with `core/search`. The pipe reads `value` once, when it is created, so a value set from outside (a reset)
 * needs a new pipe epoch (see `pipe-track.ts`).
 */
export function adminSearchPipe(name: string, value: string | null | undefined): KbqPipe {
    return {
        name,
        id: ADMIN_SEARCH_PIPE_ID,
        type: KbqPipeTypes.Input,
        value: value || null,
        cleanable: true,
        removable: false,
        disabled: false,
    };
}

/** The query from the search pipe's value. */
export function adminSearchQuery(value: unknown): string {
    return typeof value === 'string' ? value : '';
}
