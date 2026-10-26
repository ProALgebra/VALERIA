import {
    afterEveryRender,
    computed,
    DestroyRef,
    Directive,
    ElementRef,
    inject,
    Injectable,
    input,
    NgZone,
    signal,
    Signal,
} from '@angular/core';
import { CPANEL_WIDTH, cpanelWidthKey, UiStateService } from '../../core/state/ui-state.service';

/** Width of the expanded vertical navbar (Koobiq `.kbq-vertical-navbar__container.kbq-expanded`). */
export const NAV_WIDTH = 240;
/** Content next to a side panel keeps at least this width (SPEC §6.4). */
export const MIN_CONTENT_WIDTH = 480;

/** Window width as a signal (updated on `resize`, once per animation frame). */
@Injectable({ providedIn: 'root' })
export class ViewportService {
    private readonly _width = signal(typeof window === 'undefined' ? 1440 : window.innerWidth);
    readonly width = this._width.asReadonly();

    constructor() {
        if (typeof window === 'undefined') return;
        let frame = 0;
        inject(NgZone).runOutsideAngular(() =>
            window.addEventListener('resize', () => {
                cancelAnimationFrame(frame);
                frame = requestAnimationFrame(() => this._width.set(window.innerWidth));
            }),
        );
    }
}

/**
 * Width actually applied to a side panel: the saved value, or the minimum when the window is narrower than
 * panel + 480 px of content. The saved value is never overwritten by squeezing.
 */
export function effectivePanelWidth(saved: number, min: number, windowWidth: number): number {
    return windowWidth - NAV_WIDTH < saved + MIN_CONTENT_WIDTH ? min : saved;
}

/**
 * Widest a panel may be dragged in this window: content keeps 480 px, so a drag never ends in the squeezed
 * state (which would snap the panel to the minimum). Never below `min`.
 */
export function panelMaxWidth(min: number, max: number, windowWidth: number): number {
    return Math.max(min, Math.min(max, windowWidth - NAV_WIDTH - MIN_CONTENT_WIDTH));
}

export interface CpanelWidth {
    /** Bind to `kbq-content-panel-container [width]`. */
    readonly width: Signal<number>;
    readonly min: number;
    /** Bind to `[maxWidth]`: the 760 px bound, narrowed so the content keeps 480 px in this window. */
    readonly max: Signal<number>;
    /** True while the panel is squeezed to the minimum by a narrow window. */
    readonly squeezed: Signal<boolean>;
    /** Persists a width the user dragged to (clamped to min…max). */
    save(width: number): void;
}

/**
 * Content-panel width for a screen, persisted as `valeria.cpanel.<screen>.width`. Call in an injection context:
 *
 * ```ts
 * readonly panel = cpanelWidth('users');
 * ```
 * ```html
 * <kbq-content-panel-container [width]="panel.width()" [minWidth]="panel.min" [maxWidth]="panel.max()">
 *   …
 *   <kbq-content-panel [vCpanelWidth]="panel">…</kbq-content-panel>
 * </kbq-content-panel-container>
 * ```
 */
export function cpanelWidth(screen: string): CpanelWidth {
    const saved = inject(UiStateService).state(cpanelWidthKey(screen));
    const viewport = inject(ViewportService);
    const { min } = CPANEL_WIDTH;
    const max = computed(() => panelMaxWidth(min, CPANEL_WIDTH.max, viewport.width()));
    const width = computed(() => effectivePanelWidth(saved(), min, viewport.width()));
    const squeezed = computed(() => width() !== saved());
    return {
        width,
        min,
        max,
        squeezed,
        save: (px: number) => saved.set(Math.round(Math.min(max(), Math.max(min, px)))),
    };
}

/**
 * Put on `kbq-content-panel` (or any element inside it): watches the Koobiq panel wrapper
 * (`.kbq-content-panel-container__panel`, which the built-in resizer resizes) with a `ResizeObserver`, debounced
 * 300 ms, and saves user-made width changes. The container has no width output in Koobiq 20.4.2, hence the
 * observer (SPEC §6.4). The wrapper is rendered only while the panel is open (`@if (openedState())`), so it is
 * looked up after every render and re-attached when the panel opens again.
 */
@Directive({ selector: '[vCpanelWidth]' })
export class CpanelWidthDirective {
    readonly panel = input.required<CpanelWidth>({ alias: 'vCpanelWidth' });

    constructor() {
        const host = inject<ElementRef<HTMLElement>>(ElementRef).nativeElement;
        const zone = inject(NgZone);
        let timer: ReturnType<typeof setTimeout> | null = null;
        let observer: ResizeObserver | null = null;
        let target: HTMLElement | null = null;

        const stop = () => {
            observer?.disconnect();
            observer = null;
            target = null;
            if (timer) clearTimeout(timer);
            timer = null;
        };

        afterEveryRender({
            read: () => {
                const wrapper = host.closest<HTMLElement>('.kbq-content-panel-container__panel');
                if (wrapper === target) return;
                stop();
                if (!wrapper || typeof ResizeObserver === 'undefined') return;
                target = wrapper;
                zone.runOutsideAngular(() => {
                    observer = new ResizeObserver((entries) => {
                        const box = entries[entries.length - 1].borderBoxSize?.[0];
                        const observed = Math.round(box ? box.inlineSize : wrapper.getBoundingClientRect().width);
                        if (timer) clearTimeout(timer);
                        timer = setTimeout(() => {
                            const panel = this.panel();
                            if (panel.squeezed() || observed === panel.width() || observed <= 0) return;
                            zone.run(() => panel.save(observed));
                        }, 300);
                    });
                    observer.observe(wrapper, { box: 'border-box' });
                });
            },
        });

        inject(DestroyRef).onDestroy(stop);
    }
}
