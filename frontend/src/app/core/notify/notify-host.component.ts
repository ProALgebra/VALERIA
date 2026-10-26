import { ChangeDetectionStrategy, Component, inject, TemplateRef, viewChild, afterNextRender } from '@angular/core';
import { KbqLinkModule } from '@koobiq/components/link';
import { NotifyService } from './notify.service';

/** Owns toast templates (the «Открыть» action). Rendered once in the app root. */
@Component({
    selector: 'v-notify-host',
    imports: [KbqLinkModule],
    template: `
        <ng-template #open let-toast>
            <a
                kbq-link
                pseudo
                tabindex="0"
                (click)="toast.data.open?.(); toast.close()"
                (keydown.enter)="toast.data.open?.(); toast.close()"
                >{{ toast.data.openLabel }}</a
            >
        </ng-template>
    `,
    changeDetection: ChangeDetectionStrategy.OnPush,
})
export class NotifyHostComponent {
    private readonly open = viewChild.required<TemplateRef<unknown>>('open');

    constructor() {
        const notify = inject(NotifyService);
        afterNextRender(() => notify.registerOpenTemplate(this.open()));
    }
}
