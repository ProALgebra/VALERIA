import { ChangeDetectionStrategy, Component, input } from '@angular/core';
import type { RunState } from './stats-math';

/** Mark shapes (prototype `SHAPE`): one per task type, «?» for questions. */
export type ShapeName =
    | 'triangle'
    | 'square'
    | 'diamond'
    | 'down'
    | 'circle'
    | 'pentagon'
    | 'hexagon'
    | 'star'
    | 'plus'
    | 'right'
    | 'left'
    | 'cross'
    | 'question';

/**
 * Shapes handed to tasks in a fixed order: the prototype's five first, then extra ones, so up to 12 task types never share a
 * shape. Questions always get «?».
 */
export const TASK_SHAPES: ShapeName[] = ['triangle', 'square', 'diamond', 'down', 'circle', 'pentagon', 'hexagon', 'star', 'plus', 'right', 'left', 'cross'];

/**
 * The prototype ties shapes to the kind of task: CI ▲, MR review ■, release notes ◆, duplicate search ▼,
 * YouTrack ●. A task whose title names one of these kinds gets that shape; the rest take the free shapes
 * in title order. Same tasks in, same shapes out, whatever order the API returns them in.
 */
const KIND_SHAPES: [RegExp, ShapeName][] = [
    [/дубликат|duplicate/i, 'down'],
    [/(^|[^a-zа-яё])ci([^a-zа-яё]|$)/i, 'triangle'],
    [/merge request|(^|[^a-zа-яё])mr([^a-zа-яё]|$)/i, 'square'],
    [/release notes/i, 'diamond'],
    [/youtrack/i, 'circle'],
];

export function assignTaskShapes(tasks: readonly { id: string; title: string }[]): Record<string, ShapeName> {
    const sorted = [...tasks].sort((a, b) => a.title.localeCompare(b.title, 'ru') || a.id.localeCompare(b.id));
    const out: Record<string, ShapeName> = {};
    const used = new Set<ShapeName>();
    for (const t of sorted) {
        const kind = KIND_SHAPES.find(([re, shape]) => !used.has(shape) && re.test(t.title));
        if (kind) {
            out[t.id] = kind[1];
            used.add(kind[1]);
        }
    }
    let i = 0;
    for (const t of sorted) {
        if (out[t.id]) continue;
        const free = TASK_SHAPES.filter((s) => !used.has(s));
        out[t.id] = free.length ? free[0] : TASK_SHAPES[i++ % TASK_SHAPES.length];
        used.add(out[t.id]);
    }
    return out;
}

/**
 * A 12×12 glyph: `<svg vShape="triangle" st="done">`. Fill follows the run outcome via `--kbq-*` tokens:
 * done = theme, failed = error, cancelled = tertiary, running/queued = hollow.
 */
@Component({
    selector: 'svg[vShape]',
    changeDetection: ChangeDetectionStrategy.OnPush,
    host: {
        viewBox: '0 0 12 12',
        'aria-hidden': 'true',
        focusable: 'false',
        '[class]': '"v-shp v-st--" + st()',
    },
    template: `
        @switch (vShape()) {
            @case ('triangle') {
                <svg:polygon points="6,1 11.5,11 .5,11" />
            }
            @case ('square') {
                <svg:rect x="1.5" y="1.5" width="9" height="9" rx="1" />
            }
            @case ('diamond') {
                <svg:polygon points="6,.3 11.7,6 6,11.7 .3,6" />
            }
            @case ('down') {
                <svg:polygon points=".5,1 11.5,1 6,11" />
            }
            @case ('circle') {
                <svg:circle cx="6" cy="6" r="5" />
            }
            @case ('pentagon') {
                <svg:polygon points="6,.7 11.3,4.6 9.3,10.8 2.7,10.8 .7,4.6" />
            }
            @case ('hexagon') {
                <svg:polygon points="6,.5 10.8,3.25 10.8,8.75 6,11.5 1.2,8.75 1.2,3.25" />
            }
            @case ('star') {
                <svg:polygon points="6,.4 7.5,4.3 11.7,4.5 8.4,7.1 9.5,11.2 6,8.9 2.5,11.2 3.6,7.1 .3,4.5 4.5,4.3" />
            }
            @case ('plus') {
                <svg:polygon points="4,1 8,1 8,4 11,4 11,8 8,8 8,11 4,11 4,8 1,8 1,4 4,4" />
            }
            @case ('right') {
                <svg:polygon points="1,.5 11.5,6 1,11.5" />
            }
            @case ('left') {
                <svg:polygon points="11,.5 .5,6 11,11.5" />
            }
            @case ('cross') {
                <svg:polygon points="2.5,1 6,4.5 9.5,1 11,2.5 7.5,6 11,9.5 9.5,11 6,7.5 2.5,11 1,9.5 4.5,6 1,2.5" />
            }
            @case ('question') {
                <!-- Drawn, not typed: a text node would leak «?» into the «Тип» option text the pipe shows as its value. -->
                <svg:path class="v-q v-q--halo" [attr.d]="Q" />
                <svg:path class="v-q" [attr.d]="Q" />
                <svg:circle class="v-q-dot" cx="6" cy="10.7" r="1.3" />
            }
        }
    `,
    styles: `
        :host {
            width: 12px;
            height: 12px;
            display: inline-block;
            vertical-align: -1px;
            overflow: visible;
            flex: none;
            fill: currentColor;
            stroke: var(--kbq-background-bg);
            stroke-width: 1.5;
            paint-order: stroke;
        }
        .v-q { fill: none; stroke: currentColor; stroke-width: 2.2; stroke-linecap: round; paint-order: normal; }
        .v-q--halo { stroke: var(--kbq-background-bg); stroke-width: 4.2; }
        :host(.v-st--done), :host(.v-st--running), :host(.v-st--queued) { color: var(--kbq-background-theme); }
        :host(.v-st--failed) { color: var(--kbq-background-error); }
        :host(.v-st--cancelled) { color: var(--kbq-foreground-contrast-tertiary); }
        :host(.v-st--running), :host(.v-st--queued) { fill: var(--kbq-background-bg); stroke: currentColor; stroke-width: 1.5; }
        :host(.v-st--running) .v-q-dot, :host(.v-st--queued) .v-q-dot { fill: currentColor; stroke: var(--kbq-background-bg); }
        :host(.v-st--running) .v-q, :host(.v-st--queued) .v-q { stroke: currentColor; }
        :host(.v-st--running) .v-q--halo, :host(.v-st--queued) .v-q--halo { stroke: var(--kbq-background-bg); }
        :host(.v-st--key) { color: var(--kbq-foreground-contrast-secondary); }
    `,
})
export class ShapeComponent {
    readonly vShape = input.required<ShapeName>();
    readonly st = input<RunState | 'key'>('done');
    protected readonly Q = 'M3.4 4.1A2.6 2.6 0 1 1 7.4 6.3C6.5 6.9 6 7.3 6 8.2';
}
