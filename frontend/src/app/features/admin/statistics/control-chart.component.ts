import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { KbqToolTipModule } from '@koobiq/components/tooltip';
import { durShort } from '../config-shared/format';
import { ShapeComponent, ShapeName } from './shape.component';
import {
    bucketTooltip,
    buckets,
    chartNotes,
    ChartRun,
    runTooltip,
    workLines,
    xPct,
    xTicks,
    yScale,
} from './stats-math';

/**
 * Control chart (SPEC §10.1, prototype `controlChart`). x — when the run was queued, y — agent minutes,
 * the stem above a mark — queue wait. Shape = type, colour = outcome. Dashed median and 90% lines,
 * a histogram on the same x axis below, labels written on the chart instead of a legend.
 *
 * Plain HTML/CSS + tiny SVG glyphs, like the prototype: colours are `var(--kbq-*)`, so the chart follows the theme.
 */
@Component({
    selector: 'v-control-chart',
    imports: [ShapeComponent, KbqToolTipModule],
    changeDetection: ChangeDetectionStrategy.OnPush,
    template: `
        @let m = model();
        <div class="v-cc" [class.v-cc--dense]="m.dense" role="img" [attr.aria-label]="m.aria">
            <div class="v-cc__y" aria-hidden="true">
                @for (v of m.y.ticks; track v) {
                    <span [style.bottom.%]="Y(v)">{{ v ? v + ' мин' : '0' }}</span>
                }
            </div>
            <div class="v-cc__plot">
                @for (v of m.y.ticks; track v) {
                    <i class="v-cc__grid" [style.bottom.%]="Y(v)"></i>
                }
                @if (m.p90) {
                    <i class="v-cc__line is-p90" [style.bottom.%]="Y(m.p90)"></i>
                    <span class="v-cc__linelab is-p90" [style.bottom.%]="Y(m.p90)">90% быстрее {{ short(m.p90) }}</span>
                }
                @if (m.median) {
                    <i class="v-cc__line is-med" [style.bottom.%]="Y(m.median)"></i>
                    <span class="v-cc__linelab is-med" [style.bottom.%]="Y(m.median)">медиана {{ short(m.median) }}</span>
                }
                @for (p of m.points; track p.id) {
                    @if (p.stem) {
                        <i
                            class="v-cc__stem"
                            [class]="'v-cc__stem v-st--' + p.st"
                            [style.left.%]="p.x"
                            [style.bottom.%]="p.y"
                            [style.height.%]="p.stem"
                        ></i>
                    }
                    <span class="v-cc__pt" [style.left.%]="p.x" [style.bottom.%]="p.y" [kbqTooltip]="p.tt">
                        <svg [vShape]="p.shape" [st]="p.st"></svg>
                    </span>
                }
                @for (n of m.notes; track n.text) {
                    <span class="v-cc__note" [class.is-r]="n.x > 60" [style.left.%]="n.x" [style.bottom.%]="n.y">{{ n.text }}</span>
                }
            </div>
            <div class="v-cc__y v-cc__y--hist" aria-hidden="true">
                <span style="bottom: 100%">{{ m.bmax }}</span><span style="bottom: 0">0</span>
            </div>
            <div class="v-hist">
                @for (b of m.bars; track b.key) {
                    <span class="v-hist__col" [style.left.%]="b.left" [style.width.%]="b.width" [kbqTooltip]="b.tt">
                        <span class="v-hist__bar" [style.height.%]="b.height">
                            @if (b.cancelled) {
                                <i class="v-st--cancelled" [style.flex]="b.cancelled"></i>
                            }
                            @if (b.failed) {
                                <i class="v-st--failed" [style.flex]="b.failed"></i>
                            }
                            @if (b.ok) {
                                <i class="v-st--done" [style.flex]="b.ok"></i>
                            }
                            @if (b.isMax) {
                                <b class="v-hist__v">{{ b.n }}</b>
                            }
                        </span>
                    </span>
                }
            </div>
            <div></div>
            <div class="v-cc__x" aria-hidden="true">
                @for (t of m.x; track t.t) {
                    <span [style.left.%]="t.pos">{{ t.label }}</span>
                }
            </div>
        </div>
    `,
    styles: `
        :host { display: block; }
        .v-cc {
            display: grid;
            grid-template-columns: 52px minmax(0, 1fr);
            grid-template-rows: 340px 100px 32px;
            column-gap: var(--kbq-size-s);
            row-gap: 0;
            margin-right: 132px;
        }
        .v-cc__y, .v-cc__x {
            position: relative;
            color: var(--kbq-foreground-contrast-tertiary);
            font-variant-numeric: tabular-nums;
            font-size: var(--kbq-typography-text-compact-font-size);
            line-height: var(--kbq-typography-text-compact-line-height);
        }
        .v-cc__y span { position: absolute; right: 0; transform: translateY(50%); white-space: nowrap; }
        .v-cc__y--hist span:first-child { transform: translateY(100%); }
        .v-cc__plot { position: relative; border-bottom: 1px solid var(--kbq-line-contrast-fade); }
        .v-cc__grid { position: absolute; left: 0; right: 0; height: 1px; background: var(--kbq-line-contrast-less); }
        .v-cc__line { position: absolute; left: 0; right: -132px; border-top: 1px dashed var(--kbq-foreground-contrast-secondary); opacity: 0.7; }
        .v-cc__line.is-p90 { opacity: 0.45; }
        .v-cc__linelab {
            position: absolute;
            left: calc(100% + 8px);
            transform: translateY(-2px);
            font-size: var(--kbq-typography-text-compact-font-size);
            line-height: var(--kbq-typography-text-compact-line-height);
            white-space: nowrap;
            color: var(--kbq-foreground-contrast-secondary);
            background: var(--kbq-background-bg);
            padding-right: var(--kbq-size-xxs);
        }
        .v-cc__linelab.is-med { transform: translateY(calc(100% + 2px)); }
        .v-cc__stem { position: absolute; width: 2px; margin-left: -1px; border-radius: 1px; opacity: 0.45; background: currentColor; }
        .v-cc__pt {
            position: absolute;
            width: 20px;
            height: 20px;
            margin: 0 0 -10px -10px;
            display: flex;
            align-items: center;
            justify-content: center;
            cursor: default;
            z-index: 1;
        }
        .v-cc__pt:hover { z-index: 5; }
        .v-cc__pt:hover svg { transform: scale(1.5); }
        .v-cc--dense svg { width: 9px; height: 9px; }
        .v-cc--dense .v-cc__stem { width: 1px; margin-left: 0; }
        .v-cc__note {
            position: absolute;
            margin: 0 0 6px 10px;
            font-size: var(--kbq-typography-text-compact-font-size);
            line-height: var(--kbq-typography-text-compact-line-height);
            white-space: nowrap;
            color: var(--kbq-foreground-contrast);
            font-weight: var(--kbq-typography-text-compact-medium-font-weight);
            pointer-events: none;
            z-index: 2;
            text-shadow: 0 0 3px var(--kbq-background-bg), 0 0 3px var(--kbq-background-bg);
        }
        .v-cc__note.is-r { transform: translateX(calc(-100% - 20px)); }
        .v-cc__x { margin-top: var(--kbq-size-s); }
        .v-cc__x span { position: absolute; transform: translateX(-50%); white-space: nowrap; }
        .v-hist, .v-cc__y--hist { margin-top: 28px; }
        .v-hist { position: relative; border-bottom: 1px solid var(--kbq-line-contrast-fade); }
        .v-hist__col { position: absolute; top: 0; bottom: 0; display: flex; align-items: flex-end; padding: 0 1px; cursor: default; box-sizing: border-box; }
        .v-hist__col:hover { background: var(--kbq-states-background-transparent-hover); }
        .v-hist__bar { position: relative; width: 100%; display: flex; flex-direction: column; border-radius: 3px 3px 0 0; overflow: visible; gap: 1px; }
        .v-hist__bar i { display: block; min-height: 1px; background: currentColor; }
        .v-hist__bar i:first-child { border-radius: 3px 3px 0 0; }
        .v-hist__v {
            position: absolute;
            bottom: 100%;
            left: 50%;
            transform: translateX(-50%);
            margin-bottom: 2px;
            font-size: var(--kbq-typography-text-compact-font-size);
            line-height: var(--kbq-typography-text-compact-line-height);
            font-weight: var(--kbq-typography-text-compact-strong-font-weight);
            font-variant-numeric: tabular-nums;
        }
        .v-st--done, .v-st--running, .v-st--queued { color: var(--kbq-background-theme); }
        .v-st--failed { color: var(--kbq-background-error); }
        .v-st--cancelled { color: var(--kbq-foreground-contrast-tertiary); }
    `,
})
export class ControlChartComponent {
    readonly runs = input.required<ChartRun[]>();
    readonly from = input.required<number>();
    readonly to = input.required<number>();
    readonly now = input.required<number>();
    readonly shapes = input.required<Record<string, ShapeName>>();

    private readonly yMax = computed(() => yScale(this.runs()).max);

    protected readonly Y = (minutes: number): number => Math.min((minutes / this.yMax()) * 100, 100);
    protected readonly short = durShort;

    protected readonly model = computed(() => {
        const runs = this.runs();
        const from = this.from();
        const to = this.to();
        const now = this.now();
        const shapes = this.shapes();
        const y = yScale(runs);
        const Y = (m: number) => Math.min((m / y.max) * 100, 100);
        const X = (t: number) => xPct(t, from, to);
        const { median, p90 } = workLines(runs);
        const points = runs.map((r) => {
            const top = Y(r.wait + r.work);
            const base = Y(r.work);
            return {
                id: r.id,
                x: X(r.t),
                y: base,
                stem: r.wait >= 0.5 ? top - base : 0,
                st: r.st,
                shape: shapes[r.shapeKey] ?? ('circle' as ShapeName),
                tt: runTooltip(r, now),
            };
        });
        const notes = chartNotes(runs).map((n) => ({ text: n.text, x: X(n.run.t), y: Y(n.run.wait + n.run.work) }));
        const spanMin = (to - from) / 60_000;
        const list = buckets(runs, from, to);
        const bmax = Math.max(1, ...list.map((b) => b.n));
        const maxB = list.find((b) => b.n === bmax);
        const bars = list.map((b) => {
            const left = Math.max(X(b.start), 0);
            const right = Math.min(X(b.end), 100);
            return {
                key: b.start,
                left,
                width: right - left,
                height: (b.n / bmax) * 100,
                ok: b.ok,
                failed: b.failed,
                cancelled: b.cancelled,
                n: b.n,
                isMax: b === maxB,
                tt: bucketTooltip(b, spanMin),
            };
        });
        const x = xTicks(from, to).map((t) => ({ ...t, pos: X(t.t) }));
        const aria = `Контрольная карта: ${runs.length} запусков, медиана ${durShort(median)}, 90% быстрее ${durShort(p90)}`;
        return { y, median, p90, points, notes, bars, bmax, x, dense: runs.length > 250, aria };
    });
}
