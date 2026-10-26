import { inject, Injectable, TemplateRef } from '@angular/core';
import { Router } from '@angular/router';
import { KbqToastData, KbqToastService, KbqToastStyle } from '@koobiq/components/toast';
import { isAccessChanged } from '../api/api-error';
import { Item, itemRoute } from '../items/item.model';
import { isWaiting, statusLabel } from '../status/status';

/** Extra data read by the «Открыть» action template (`toast.data.open()`). */
export interface ToastWithOpen extends KbqToastData {
    open?: () => void;
    openLabel?: string;
}

const DURATION_MS = 4000;

/**
 * Toasts through `kbq-toast` (bottom right, see app.config). Prototype default kind is success.
 * Text goes to the toast caption.
 */
@Injectable({ providedIn: 'root' })
export class NotifyService {
    private readonly toast = inject(KbqToastService);
    private readonly router = inject(Router);
    private openTemplate: TemplateRef<unknown> | null = null;

    /** Registered once by `NotifyHostComponent` (it owns the «Открыть» template). */
    registerOpenTemplate(template: TemplateRef<unknown>): void {
        this.openTemplate = template;
    }

    success(title: string, text?: string): void {
        this.show(KbqToastStyle.Success, title, text);
    }

    /** Pass the caught error as `cause`: a 403 that changed the user's access shows no toast (SPEC §16.2). */
    error(title: string, text?: string, cause?: unknown): void {
        if (isAccessChanged(cause)) return;
        this.show(KbqToastStyle.Error, title, text);
    }

    warning(title: string, text?: string): void {
        this.show(KbqToastStyle.Warning, title, text);
    }

    /** Neutral toast («Вопрос отправлен», «Скопировано»). */
    info(title: string, text?: string): void {
        this.show(KbqToastStyle.Contrast, title, text);
    }

    /** Toast with an «Открыть» action. */
    withOpen(style: KbqToastStyle, title: string, text: string | undefined, open: () => void): void {
        const data: ToastWithOpen = {
            style,
            title,
            caption: text,
            closeButton: true,
            actions: this.openTemplate ?? undefined,
            open,
            openLabel: 'Открыть',
        };
        this.toast.show(data, DURATION_MS * 2);
    }

    /**
     * Final result from SSE (SPEC §7): «Ответ готов: …», «Задача завершена: …», «Valeria ждёт вашего ответа: …»
     * with «Открыть». Nothing when the user is already on that chat.
     */
    finalResult(item: Item): void {
        const route = itemRoute(item);
        if (this.isOn(route)) return;
        const open = () => void this.router.navigateByUrl(route);
        if (isWaiting(item.status)) {
            this.withOpen(KbqToastStyle.Warning, `Valeria ждёт вашего ответа: ${item.title}`, undefined, open);
        } else if (item.status === 'failed') {
            this.withOpen(KbqToastStyle.Error, `${statusLabel('failed')}: ${item.title}`, undefined, open);
        } else if (item.status === 'completed' || item.status === 'idle') {
            const title = item.kind === 'task' ? `Задача завершена: ${item.title}` : `Ответ готов: ${item.title}`;
            this.withOpen(KbqToastStyle.Success, title, undefined, open);
        }
    }

    private isOn(route: string): boolean {
        const path = this.router.url.split(/[?#]/)[0];
        return path === route;
    }

    private show(style: KbqToastStyle, title: string, text?: string): void {
        this.toast.show({ style, title, caption: text, closeButton: true }, DURATION_MS);
    }
}
