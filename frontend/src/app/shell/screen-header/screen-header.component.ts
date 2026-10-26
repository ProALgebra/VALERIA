import { ChangeDetectionStrategy, Component, computed, inject, input } from '@angular/core';
import { NgTemplateOutlet } from '@angular/common';
import { RouterLink } from '@angular/router';
import { KbqBreadcrumbsModule } from '@koobiq/components/breadcrumbs';
import { KbqButtonModule } from '@koobiq/components/button';
import { KbqComponentColors, PopUpPlacements } from '@koobiq/components/core';
import { KbqDividerModule } from '@koobiq/components/divider';
import { KbqIconModule } from '@koobiq/components/icon';
import { KbqToolTipModule } from '@koobiq/components/tooltip';
import { KbqTopBarModule } from '@koobiq/components/top-bar';
import { Item } from '../../core/items/item.model';
import { UI_KEYS, UiStateService } from '../../core/state/ui-state.service';
import { ChatMenuComponent } from '../chat-menu/chat-menu.component';

export interface Crumb {
    label: string;
    link: string;
}

/**
 * Screen header over `kbq-top-bar` (SPEC §6.3).
 *
 * ```html
 * <v-screen-header title="Пользователи" [crumbs]="[{ label: 'Система', link: '/admin/statistics' }]">
 *   <span vHeaderLead><!-- mode icon or tag --></span>
 *   <ng-container vHeaderActions><!-- screen actions --></ng-container>
 * </v-screen-header>
 * ```
 * `[chat]` adds the chat menu «…»; `[asideToggle]="true"` adds «Скрыть панель» / «Показать панель»
 * bound to `valeria.chat.aside.open`.
 * With `[crumbs]` the title is the last breadcrumb (stock Big breadcrumbs) and `vHeaderLead` is not shown;
 * without crumbs it is an `h1` with the lead before it.
 */
@Component({
    selector: 'v-screen-header',
    imports: [
        NgTemplateOutlet,
        KbqTopBarModule,
        KbqBreadcrumbsModule,
        KbqButtonModule,
        KbqDividerModule,
        KbqIconModule,
        KbqToolTipModule,
        RouterLink,
        ChatMenuComponent,
    ],
    changeDetection: ChangeDetectionStrategy.OnPush,
    host: { class: 'v-screen-header' },
    template: `
        <kbq-top-bar>
            <div kbqTopBarContainer placement="start" class="v-screen-header__start">
                <ng-content select="[vHeaderBack]" />
                @if (crumbs().length) {
                    <!-- Stock Big breadcrumbs; the screen title is the last (current) item, in the stock style. -->
                    <nav kbq-breadcrumbs size="big" class="v-screen-header__crumbs">
                        @for (crumb of crumbs(); track crumb.link) {
                            <kbq-breadcrumb-item [routerLink]="crumb.link" [text]="crumb.label" />
                        }
                        <kbq-breadcrumb-item [text]="title()" current />
                    </nav>
                } @else {
                    <ng-container *ngTemplateOutlet="heading" />
                }
                <span class="v-screen-header__after"><ng-content select="[vHeaderAfterTitle]" /></span>
            </div>
            <div kbqTopBarSpacer></div>
            <div kbqTopBarContainer placement="end">
                <ng-content select="[vHeaderActions]" />
                @if (chat(); as item) {
                    <v-chat-menu [item]="item" appearance="header" />
                }
                @if (asideToggle()) {
                    <button
                        kbq-button
                        kbqStyle="transparent"
                        [color]="colors.Contrast"
                        [class.kbq-active]="asideOpen()"
                        [kbqTooltip]="asideLabel()"
                        [kbqPlacement]="placements.Bottom"
                        [attr.aria-label]="asideLabel()"
                        [attr.aria-pressed]="asideOpen()"
                        (click)="toggleAside()"
                    >
                        <i kbq-icon="kbq-split-screen-right_16"></i>
                    </button>
                }
            </div>
        </kbq-top-bar>
        <!-- No paddings: the stock divider adds 4px above and below, a strip between the bar and the content. -->
        <kbq-divider [paddings]="false" />

        <ng-template #heading>
            <span class="v-screen-header__heading">
                <ng-content select="[vHeaderLead]" />
                <h1 class="kbq-title v-screen-header__title">{{ title() }}</h1>
            </span>
        </ng-template>
    `,
    styles: `
        :host { display: block; flex: none; }
        /* One height on every screen: the start container is as tall as a button (--kbq-size-3xl), so a header
           with only a title is as tall as one with actions (stock top bar: 12 + 32 + 12). */
        .v-screen-header__start { align-items: center; min-width: 0; min-height: var(--kbq-size-3xl); }
        .v-screen-header__crumbs { min-width: 0; }
        .v-screen-header__heading { display: inline-flex; align-items: center; gap: var(--kbq-size-s); min-width: 0; }
        .v-screen-header__title {
            margin: 0;
            min-width: 0;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }
        /* Prototype .v-titlerow: 12px between the title and what follows it (badge, tag, note). */
        .v-screen-header__after { display: inline-flex; flex: none; align-items: center; gap: var(--kbq-size-m); margin-left: var(--kbq-size-m); }
        .v-screen-header__after:empty { display: none; }
    `,
})
export class ScreenHeaderComponent {
    readonly title = input('');
    readonly crumbs = input<Crumb[]>([]);
    /** Chat whose menu «…» is shown on the right. */
    readonly chat = input<Item | null | undefined>(null);
    /** Shows the «Скрыть панель» / «Показать панель» toggle for the chat aside. */
    readonly asideToggle = input(false);

    protected readonly colors = KbqComponentColors;
    protected readonly placements = PopUpPlacements;
    protected readonly asideOpen = inject(UiStateService).state(UI_KEYS.chatAsideOpen);
    protected readonly asideLabel = computed(() => (this.asideOpen() ? 'Скрыть панель' : 'Показать панель'));

    protected toggleAside(): void {
        this.asideOpen.update((open) => !open);
    }
}
