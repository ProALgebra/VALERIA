import { ChangeDetectionStrategy, Component, computed, inject, signal } from '@angular/core';
import { Router, RouterLink } from '@angular/router';
import { KbqBadgeColors, KbqBadgeModule } from '@koobiq/components/badge';
import { KbqButtonModule } from '@koobiq/components/button';
import { KbqComponentColors } from '@koobiq/components/core';
import { KbqIconModule } from '@koobiq/components/icon';
import { KbqLinkModule } from '@koobiq/components/link';
import { PageStateComponent } from '../../../shared/page-state/page-state.component';
import { ScreenHeaderComponent } from '../../../shell/screen-header/screen-header.component';
import { AdminConfigApi, AdminTask, isOn } from '../config-shared/admin-config.api';
import { ADMIN_GRID } from '../shared/admin-grid';
import { changedLabel, EMPTY, plural } from '../config-shared/format';
import { sortByName } from '../config-shared/names';
import { audienceLabel } from './task-form';

const users = (n: number) => plural(n, 'пользователь', 'пользователя', 'пользователей');

/** «Настройка задач» list (prototype `ataskView` list): a row opens the full-page form. */
@Component({
    selector: 'v-task-list',
    imports: [ScreenHeaderComponent, PageStateComponent, ADMIN_GRID, KbqBadgeModule, KbqButtonModule, KbqIconModule, KbqLinkModule, RouterLink],
    changeDetection: ChangeDetectionStrategy.OnPush,
    template: `
        <v-screen-header title="Настройка задач">
            <ng-container vHeaderAfterTitle>
                @if (rows()) {
                    <kbq-badge [badgeColor]="badgeColors.FadeContrast">{{ rows()!.length }}</kbq-badge>
                }
            </ng-container>
            <ng-container vHeaderActions>
                <a kbq-button [color]="colors.Contrast" routerLink="/admin/tasks/new">
                    <i kbq-icon="kbq-plus_16"></i>
                    Создать задачу
                </a>
            </ng-container>
        </v-screen-header>
        <v-page-state [loading]="loading()" [error]="error()" (retry)="load()">
            <div class="v-content">
                <v-admin-grid [rows]="rows() ?? []" [rowId]="rowId" openable noRowsText="Задач пока нет" (rowOpen)="open($event.id)">
                    <ng-template vCol="title" header="Название" [flex]="1" [minWidth]="200" let-r>
                        <a kbq-link class="v-title" [routerLink]="['/admin/tasks', r.id]">{{ r.title }}</a>
                    </ng-template>
                    <ng-template vCol="status" header="Статус" kind="line" let-r>
                        <kbq-badge [badgeColor]="r.published ? badgeColors.FadeSuccess : badgeColors.FadeContrast">
                            {{ r.published ? 'Опубликована' : 'Черновик' }}
                        </kbq-badge>
                    </ng-template>
                    <ng-template vCol="audience" header="Кто видит" kind="nowrap" let-r>{{ r.audience }}</ng-template>
                    <ng-template vCol="model" header="Модель" kind="nowrap" let-r>
                        <span class="v-mono">{{ r.model }}</span>
                    </ng-template>
                    <ng-template vCol="next" header="Затем запускается" [flex]="1" [minWidth]="160" let-r>{{ r.next }}</ng-template>
                    <ng-template vCol="changed" header="Изменена" kind="nowrap" let-r>
                        <span class="v-secondary">{{ r.changed }}</span>
                    </ng-template>
                </v-admin-grid>
            </div>
        </v-page-state>
    `,
    styles: `
        :host { display: flex; flex-direction: column; flex: 1 1 auto; min-height: 0; }
        .v-content { flex: 1 1 auto; min-height: 0; overflow: auto; padding: var(--kbq-size-l) var(--kbq-size-xxl) var(--kbq-size-xxl); }
        .v-mono { font-family: var(--kbq-typography-mono-normal-font-family); }
        .v-secondary { color: var(--kbq-foreground-contrast-secondary); }
    `,
})
export class TaskListComponent {
    private readonly api = inject(AdminConfigApi);
    private readonly router = inject(Router);

    protected readonly colors = KbqComponentColors;
    protected readonly badgeColors = KbqBadgeColors;
    protected readonly loading = signal(true);
    protected readonly error = signal<unknown>(null);
    private readonly tasks = signal<AdminTask[] | null>(null);

    protected readonly rows = computed(() => {
        const tasks = this.tasks();
        if (!tasks) return null;
        const title = new Map(tasks.map((t) => [t.id, t.title]));
        return sortByName(tasks, (t) => t.title).map((t) => ({
            id: t.id,
            title: t.title,
            published: isOn(t.is_published),
            audience: audienceLabel(t, users),
            model: t.model_id || EMPTY,
            next: (t.next_task_ids ?? []).map((id) => title.get(id)).filter(Boolean).join(', ') || EMPTY,
            changed: changedLabel(t.updated_at),
        }));
    });

    constructor() {
        void this.load();
    }

    async load(): Promise<void> {
        this.loading.set(true);
        this.error.set(null);
        try {
            this.tasks.set(await this.api.tasks());
        } catch (err) {
            this.error.set(err);
        } finally {
            this.loading.set(false);
        }
    }

    protected readonly rowId = (r: { id: string }): string => r.id;

    /** The whole row opens by click and Enter; the title link stays the middle-click target. */
    protected open(id: string): void {
        void this.router.navigate(['/admin/tasks', id]);
    }
}
