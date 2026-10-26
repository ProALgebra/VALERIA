import { inject, Injectable } from '@angular/core';
import { Router } from '@angular/router';
import { KbqComponentColors } from '@koobiq/components/core';
import { KbqModalRef, KbqModalService, ModalSize } from '@koobiq/components/modal';
import { Subject } from 'rxjs';
import { Item, itemRoute } from '../../core/items/item.model';
import { ItemsStore } from '../../core/items/items.store';
import { NotifyService } from '../../core/notify/notify.service';
import { describeError } from '../../core/status/error-codes';
import { SnoozeOption, snoozeUntil, snoozeWhenText, toIsoWithOffset } from '../../core/time/snooze';

/** Which menu a chat gets: archive wins over snooze (SPEC §6.2). */
export type ChatMenuMode = 'normal' | 'snoozed' | 'archived';

export function chatMenuMode(item: Pick<Item, 'archived_at' | 'snoozed_until'>, now = Date.now()): ChatMenuMode {
    if (item.archived_at) return 'archived';
    if (item.snoozed_until && Date.parse(item.snoozed_until) > now) return 'snoozed';
    return 'normal';
}

/**
 * Chat menu actions shared by the nav rows and the screen header: API call through {@link ItemsStore},
 * toast, and «back to home» when the action hides the chat that is open right now.
 */
@Injectable({ providedIn: 'root' })
export class ChatActionsService {
    private readonly store = inject(ItemsStore);
    private readonly notify = inject(NotifyService);
    private readonly modal = inject(KbqModalService);
    private readonly router = inject(Router);

    /** Emits after a chat was deleted; the nav moves focus to the neighbouring item. */
    readonly removed$ = new Subject<Item>();

    async snooze(item: Item, option: SnoozeOption): Promise<void> {
        const until = snoozeUntil(option);
        const wasOpen = this.isOpen(item);
        await this.run(async () => {
            await this.store.snooze(item, toIsoWithOffset(until));
            if (wasOpen) await this.router.navigateByUrl('/');
            this.notify.success(
                'Чат отложен',
                `«${item.title}» вернётся ${snoozeWhenText(until)}. До тех пор он в «Отложенных» внизу меню.`,
            );
        });
    }

    async unsnooze(item: Item): Promise<void> {
        await this.run(async () => {
            await this.store.unsnooze(item);
            this.notify.success('Возвращено из отложенных', item.title);
        });
    }

    async archive(item: Item): Promise<void> {
        const wasOpen = this.isOpen(item);
        await this.run(async () => {
            await this.store.archive(item);
            if (wasOpen) await this.router.navigateByUrl('/');
            this.notify.success('Перенесено в архив', item.title);
        });
    }

    async unarchive(item: Item): Promise<void> {
        await this.run(async () => {
            await this.store.unarchive(item);
            this.notify.success('Возвращено из архива', item.title);
        });
    }

    /**
     * «Удалить чат?» modal; focus starts on «Отмена», Esc closes. When the user backs out, focus returns to
     * `returnFocus` (the nav row: the menu item that opened the modal no longer exists).
     */
    confirmRemove(item: Item, returnFocus?: HTMLElement): void {
        let confirmed = false;
        const ref: KbqModalRef = this.modal.create({
            kbqTitle: 'Удалить чат?',
            kbqContent: `Чат «${escapeHtml(item.title)}» удалится вместе с ответами. Восстановить его не получится.`,
            kbqSize: ModalSize.Small,
            kbqClosable: true,
            kbqFooter: [
                {
                    label: 'Удалить',
                    // Filled contrast: kbq-button has no error colour, so 'error' rendered a plain grey button.
                    type: KbqComponentColors.Contrast,
                    onClick: (): void => {
                        confirmed = true;
                        ref.close();
                        void this.remove(item);
                    },
                },
                { label: 'Отмена', autoFocus: true, onClick: (): void => ref.close() },
            ],
        });
        ref.afterClose.subscribe(() => {
            const active = document.activeElement;
            if (confirmed || !returnFocus?.isConnected || (active && active !== document.body)) return;
            returnFocus.focus();
        });
    }

    async remove(item: Item): Promise<void> {
        const wasOpen = this.isOpen(item);
        await this.run(async () => {
            await this.store.remove(item);
            if (wasOpen) await this.router.navigateByUrl('/');
            this.removed$.next(item);
            this.notify.success('Чат удалён', item.title);
        });
    }

    /** True when the item's screen is the current route. */
    isOpen(item: Pick<Item, 'kind' | 'id' | 'chat_id'>): boolean {
        const path = this.router.url.split(/[?#]/)[0];
        return path === itemRoute(item);
    }

    private async run(action: () => Promise<void>): Promise<void> {
        try {
            await action();
        } catch (err) {
            const { text, code } = describeError(err);
            this.notify.error(text, code ?? undefined, err);
        }
    }
}

export function escapeHtml(value: string): string {
    return value.replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]!);
}
