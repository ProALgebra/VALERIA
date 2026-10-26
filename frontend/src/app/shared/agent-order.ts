/** Mode toggles list agents as in the prototype: Plan, then Build, then anything else in server order. */
export function planFirst(agents: readonly string[]): string[] {
    const rank = (a: string) => ['plan', 'build'].indexOf(a) + 1 || 3;
    return [...agents].sort((a, b) => rank(a) - rank(b));
}
