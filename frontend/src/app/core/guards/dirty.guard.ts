import { inject, Injectable } from '@angular/core';
import { CanDeactivateFn } from '@angular/router';
import { KbqModalRef, KbqModalService, ModalSize } from '@koobiq/components/modal';

/** A routed component may implement this; forms can also register through {@link DirtyFormService.track}. */
export interface DirtyAware {
    isDirty(): boolean;
}

/**
 * One registry of unsaved forms for the whole app (SPEC §15, pattern «Предупреждение об уходе с несохранённой формой»):
 * the router guard asks before leaving, `beforeunload` covers closing or reloading the tab.
 */
@Injectable({ providedIn: 'root' })
export class DirtyFormService {
    private readonly modal = inject(KbqModalService);
    private readonly checks = new Set<() => boolean>();
    private pending: Promise<boolean> | null = null;

    constructor() {
        if (typeof window !== 'undefined') {
            window.addEventListener('beforeunload', (event) => {
                if (!this.isDirty()) return;
                event.preventDefault();
                event.returnValue = '';
            });
        }
    }

    /** Registers a dirty check; call the returned function on destroy. */
    track(isDirty: () => boolean): () => void {
        this.checks.add(isDirty);
        return () => this.checks.delete(isDirty);
    }

    isDirty(): boolean {
        for (const check of this.checks) if (check()) return true;
        return false;
    }

    /** Asks «Несохраненные данные будут потеряны.»; resolves true for «Все равно перейти». */
    confirmLeave(): Promise<boolean> {
        if (this.pending) return this.pending;
        this.pending = new Promise<boolean>((resolve) => {
            let result = false;
            const ref: KbqModalRef = this.modal.create({
                kbqContent: 'Несохраненные данные будут потеряны.',
                kbqSize: ModalSize.Small,
                kbqClosable: true,
                kbqFooter: [
                    {
                        label: 'Все равно перейти',
                        type: 'contrast',
                        onClick: (): void => {
                            result = true;
                            ref.close();
                        },
                    },
                    { label: 'Остаться', autoFocus: true, onClick: (): void => ref.close() },
                ],
            });
            ref.afterClose.subscribe(() => {
                this.pending = null;
                resolve(result);
            });
        });
        return this.pending;
    }
}

/** App-wide `canDeactivate`: added to every routed screen in `app.routes.ts`. */
export const dirtyGuard: CanDeactivateFn<unknown> = (component) => {
    const svc = inject(DirtyFormService);
    const own = !!component && typeof (component as Partial<DirtyAware>).isDirty === 'function' && (component as DirtyAware).isDirty();
    if (!own && !svc.isDirty()) return true;
    return svc.confirmLeave();
};
