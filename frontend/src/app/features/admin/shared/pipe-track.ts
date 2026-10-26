import { KbqPipe, KbqPipeTypes } from '@koobiq/components/filter-bar';

/** Epoch each pipe object was built in (see {@link withEpoch}). */
const epochs = new WeakMap<KbqPipe, number>();

/**
 * Stamps the screen's epoch on freshly built pipe objects. The Koobiq pipe reads its data once, when it is
 * created, and then keeps the value the user picked in its own copy, so a value set from outside (the «Сбросить»
 * button, a deep link whose labels arrive later) is not shown until the pipe is re-created. The screen bumps its
 * epoch after such an outside change, so every pipe gets a new track key and is re-created with the current data.
 *
 * The epoch lives on the pipe object, not in the track function: `@for` recomputes the key of an item it already
 * rendered with the current track function, so a key that read the epoch signal would change on both sides.
 */
export function withEpoch<T extends KbqPipe>(pipes: T[], epoch: number): T[] {
    for (const pipe of pipes) epochs.set(pipe, epoch);
    return pipes;
}

/**
 * `@for` track key for `*kbqPipe`: the pipe's epoch and id. Between epochs the key is stable, so an open
 * multi-select is not closed while the user picks values. A single-select value's label is part of the key as
 * well: when it changes, the pipe is re-created with the new data.
 */
export function pipeTrackKey(pipe: KbqPipe): string {
    const id = `${epochs.get(pipe) ?? 0}|${String(pipe.id ?? pipe.name)}`;
    if (pipe.type !== KbqPipeTypes.Select) return id;
    const value = pipe.value as { name?: unknown } | null | undefined;
    return `${id}|${value?.name ?? ''}`;
}

/** Values of a multi-select pipe as strings (`[]` when nothing is picked or every option is picked). */
export function multiSelectValues(value: unknown): string[] {
    return Array.isArray(value) ? value.map((o: { value: unknown }) => String(o.value)) : [];
}
