import { ChangeDetectionStrategy, Component, inject, input } from '@angular/core';
import { ActivatedRoute } from '@angular/router';
import { ScreenHeaderComponent } from '../../shell/screen-header/screen-header.component';

/**
 * Stand-in for a screen that is not built yet. Route data `{ screen }` names it; a screen worker replaces
 * the route's `loadComponent` with the real component. Not a user-facing text — never shipped as is.
 */
@Component({
    selector: 'v-placeholder',
    imports: [ScreenHeaderComponent],
    changeDetection: ChangeDetectionStrategy.OnPush,
    host: { 'data-placeholder': '' },
    template: `
        <v-screen-header [title]="screen()" />
        <div class="v-placeholder kbq-text-normal">
            <code>/{{ pattern }}</code>
        </div>
    `,
    styles: `
        :host { display: flex; flex-direction: column; flex: 1 1 auto; min-height: 0; }
        .v-placeholder { padding: var(--kbq-size-xxl); color: var(--kbq-foreground-contrast-tertiary); }
    `,
})
export class PlaceholderComponent {
    readonly screen = input('');
    protected readonly pattern = inject(ActivatedRoute).snapshot.routeConfig?.path ?? '';
}
