import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { RouterLink } from '@angular/router';
import { KbqButtonModule } from '@koobiq/components/button';
import { KbqComponentColors } from '@koobiq/components/core';
import { KbqEmptyStateModule } from '@koobiq/components/empty-state';
import { KbqIconModule } from '@koobiq/components/icon';
import { ScreenHeaderComponent } from '../../shell/screen-header/screen-header.component';

/** Why the section is closed: admin-only section, or folder work for a «Задачи и вопросы» account. */
export type DeniedReason = 'admin' | 'tasks-only';

export const DENIED_TEXT: Record<DeniedReason, string> = {
    admin: 'Раздел доступен только администратору.',
    'tasks-only':
        'Учётка «Задачи и вопросы» запускает задачи и задаёт вопросы. Работа в папке доступна обычным пользователям.',
};

/**
 * «Раздел недоступен» in the content area (SPEC §4, PT pattern «Роли — скрытие/блокировка»).
 * The URL stays as typed; the top bar shows the section name. Route data: `{ section, reason }`.
 */
@Component({
    selector: 'v-access-denied',
    imports: [RouterLink, KbqButtonModule, KbqEmptyStateModule, KbqIconModule, ScreenHeaderComponent],
    changeDetection: ChangeDetectionStrategy.OnPush,
    template: `
        <v-screen-header [title]="section()" />
        <div class="v-denied">
            <kbq-empty-state size="big" [alignTop]="true">
                <i kbq-empty-state-icon kbq-icon-item="kbq-lock_16" [big]="true" [fade]="true" [color]="colors.ContrastFade"></i>
                <div kbq-empty-state-title>Раздел недоступен для вашей учётки</div>
                <div kbq-empty-state-text>{{ text() }}</div>
                <div kbq-empty-state-actions>
                    <a kbq-button routerLink="/">На главную</a>
                </div>
            </kbq-empty-state>
        </div>
    `,
    styles: `
        :host { display: flex; flex-direction: column; flex: 1 1 auto; min-height: 0; }
        .v-denied { display: flex; flex: 1 1 auto; padding-top: var(--kbq-size-7xl); overflow: auto; }
    `,
})
export class AccessDeniedComponent {
    /** Section name for the top bar (route data). */
    readonly section = input('');
    readonly reason = input<DeniedReason>('admin');

    protected readonly colors = KbqComponentColors;
    protected readonly text = computed(() => DENIED_TEXT[this.reason()] ?? DENIED_TEXT.admin);
}
