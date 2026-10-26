/**
 * Object names (SPEC §15 «Названия объектов»): trim + NFC, required, ≤ 120 characters (backend limit),
 * unique ignoring case, lists sorted with `Intl.Collator` `numeric: true`.
 */
export const NAME_MAX = 120;
export const NAME_REQUIRED = 'Укажите название';
export const NAME_TOO_LONG = `Не более ${NAME_MAX} символов`;
/** Draft text: the prototype has no duplicate-name message. */
export const NAME_TAKEN = 'Такое название уже есть';

export const nameCollator = new Intl.Collator('ru', { numeric: true, sensitivity: 'base' });

export function normalizeName(value: string | null | undefined): string {
    return (value ?? '').trim().normalize('NFC');
}

const key = (value: string) => normalizeName(value).toLocaleLowerCase('ru');

/** Error text for a name, or null. `others` are the names of the other objects of the same kind. */
export function validateName(value: string | null | undefined, others: readonly string[]): string | null {
    const name = normalizeName(value);
    if (!name) return NAME_REQUIRED;
    if ([...name].length > NAME_MAX) return NAME_TOO_LONG;
    const k = key(name);
    if (others.some((o) => key(o) === k)) return NAME_TAKEN;
    return null;
}

export function sortByName<T>(items: readonly T[], name: (item: T) => string): T[] {
    return [...items].sort((a, b) => nameCollator.compare(name(a), name(b)));
}
