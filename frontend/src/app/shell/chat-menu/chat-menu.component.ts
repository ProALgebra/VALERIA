import { FocusMonitor, FocusOrigin } from '@angular/cdk/a11y';
import { ChangeDetectionStrategy, Component, computed, ElementRef, inject, input, signal, viewChild } from '@angular/core';
import { KbqButtonModule } from '@koobiq/components/button';
import { KbqComponentColors, PopUpPlacements } from '@koobiq/components/core';
import { KbqDividerModule } from '@koobiq/components/divider';
import { KbqDropdownModule, KbqDropdownTrigger } from '@koobiq/components/dropdown';
import { KbqIconModule } from '@koobiq/components/icon';
import { KbqToolTipModule } from '@koobiq/components/tooltip';
import { Item } from '../../core/items/item.model';
import { SNOOZE_OPTIONS } from '../../core/time/snooze';
import { ChatActionsService, chatMenuMode } from './chat-actions.service';

export const CHAT_MENU_LABEL = 'Действия с чатом';

/**
 * Chat menu «…» (SPEC §6.2). One component for the nav rows (`appearance="row"`, 24 px icon button that is
 * not in the tab order: the row opens it with the context-menu key or Shift+F10) and for the screen header
 * (`appearance="header"`, a regular 32 px transparent button).
 */
@Component({
    selector: 'v-chat-menu',
    exportAs: 'vChatMenu',
    imports: [KbqDropdownModule, KbqIconModule, KbqButtonModule, KbqDividerModule, KbqToolTipModule],
    changeDetection: ChangeDetectionStrategy.OnPush,
    host: { class: 'v-chat-menu', '[class.v-chat-menu_open]': 'opened()' },
    template: `
        @if (appearance() === 'header') {
            <button
                kbq-button
                kbqStyle="transparent"
                [color]="colors.Contrast"
                [kbqDropdownTriggerFor]="menu"
                [kbqTooltip]="label"
                [kbqPlacement]="placements.Bottom"
                [attr.aria-label]="label"
                (dropdownOpened)="opened.set(true)"
                (dropdownClosed)="opened.set(false)"
            >
                <i kbq-icon="kbq-ellipsis-horizontal_16"></i>
            </button>
        } @else {
            <i
                kbq-icon-button="kbq-ellipsis-horizontal_16"
                size="compact"
                role="button"
                [tabindex]="-1"
                [color]="colors.ContrastFade"
                [kbqDropdownTriggerFor]="menu"
                [kbqTooltip]="label"
                [kbqPlacement]="placements.Right"
                [attr.aria-label]="label"
                aria-haspopup="menu"
                [kbqDropdownTriggerRestoreFocus]="false"
                (click)="$event.preventDefault(); $event.stopPropagation()"
                (dropdownOpened)="onRowMenuOpened()"
                (dropdownClosed)="onRowMenuClosed()"
            ></i>
        }

        <kbq-dropdown #menu="kbqDropdown">
            @switch (mode()) {
                @case ('archived') {
                    <button kbq-dropdown-item (click)="actions.unarchive(item())">
                        <i kbq-icon="kbq-undo_16"></i>Вернуть из архива
                    </button>
                    <kbq-divider />
                    <button kbq-dropdown-item (click)="actions.confirmRemove(item(), row())">
                        <i kbq-icon="kbq-trash_16"></i>Удалить
                    </button>
                }
                @default {
                    <!-- Group caption: the stock kbq-select optgroup label (kbq-optgroup itself only works inside kbq-select). -->
                    <label class="kbq-optgroup-label">Отложить</label>
                    @if (mode() === 'snoozed') {
                        <button kbq-dropdown-item (click)="actions.unsnooze(item())">
                            <i kbq-icon="kbq-undo_16"></i>Вернуть из отложенных
                        </button>
                    } @else {
                        @for (option of snoozeOptions; track option.id) {
                            <button kbq-dropdown-item (click)="actions.snooze(item(), option.id)">{{ option.label }}</button>
                        }
                    }
                    <kbq-divider />
                    <button kbq-dropdown-item (click)="actions.archive(item())">
                        <i kbq-icon="kbq-box-archive_16"></i>В архив
                    </button>
                    <kbq-divider />
                    <button kbq-dropdown-item (click)="actions.confirmRemove(item(), row())">
                        <i kbq-icon="kbq-trash_16"></i>Удалить
                    </button>
                }
            }
        </kbq-dropdown>
    `,
    styles: `
        :host { display: inline-flex; flex: none; align-items: center; }
    `,
})
export class ChatMenuComponent {
    readonly item = input.required<Item>();
    readonly appearance = input<'row' | 'header'>('row');

    protected readonly actions = inject(ChatActionsService);
    protected readonly colors = KbqComponentColors;
    protected readonly placements = PopUpPlacements;
    protected readonly label = CHAT_MENU_LABEL;
    protected readonly snoozeOptions = SNOOZE_OPTIONS;
    protected readonly mode = computed(() => chatMenuMode(this.item()));
    readonly opened = signal(false);

    private readonly trigger = viewChild(KbqDropdownTrigger);
    private readonly host = inject<ElementRef<HTMLElement>>(ElementRef);
    private readonly focusMonitor = inject(FocusMonitor);
    private rowMenuOrigin: FocusOrigin = 'program';

    /** The nav row hosting this menu (row appearance only). */
    protected row(): HTMLElement | undefined {
        if (this.appearance() !== 'row') return undefined;
        return this.host.nativeElement.closest<HTMLElement>('.kbq-navbar-item') ?? undefined;
    }

    protected onRowMenuOpened(): void {
        this.rowMenuOrigin = this.trigger()?.openedBy ?? 'program';
        this.opened.set(true);
    }

    /**
     * The row trigger is hidden once the menu closes, so Koobiq's focus restore would drop focus on <body>.
     * Focus goes back to the row instead, unless the action already moved it (a modal, another row).
     */
    protected onRowMenuClosed(): void {
        this.opened.set(false);
        const active = document.activeElement;
        if (active && active !== document.body && !this.host.nativeElement.contains(active)) return;
        const row = this.row();
        if (row?.isConnected) this.focusMonitor.focusVia(row, this.rowMenuOrigin);
    }

    /** Opens the menu from the keyboard (context-menu key / Shift+F10 on the row). */
    openFromKeyboard(): void {
        const trigger = this.trigger();
        if (!trigger) return;
        trigger.openedBy = 'keyboard';
        trigger.open();
    }
}
