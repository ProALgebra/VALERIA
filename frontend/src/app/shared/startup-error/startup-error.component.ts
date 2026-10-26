import { ChangeDetectionStrategy, Component, inject, signal } from '@angular/core';
import { Router } from '@angular/router';
import { AuthService } from '../../core/auth/auth.service';
import { PageStateComponent } from '../page-state/page-state.component';

/**
 * Full-page error state when `/api/auth/me` fails at startup with a network error or 5xx (SPEC §7).
 * «Попробовать снова» re-reads the session and re-runs the navigation to the URL the user opened.
 */
@Component({
    selector: 'v-startup-error',
    imports: [PageStateComponent],
    changeDetection: ChangeDetectionStrategy.OnPush,
    template: `<v-page-state [error]="auth.loadError()" (retry)="retry()" />`,
    styles: `
        :host { display: flex; flex-direction: column; min-height: 100vh; }
    `,
})
export class StartupErrorComponent {
    protected readonly auth = inject(AuthService);
    private readonly router = inject(Router);
    private readonly retrying = signal(false);

    protected async retry(): Promise<void> {
        if (this.retrying()) return;
        this.retrying.set(true);
        try {
            await this.auth.refresh();
        } catch {
            return; // still failing: the error state stays with the new error
        } finally {
            this.retrying.set(false);
        }
        await this.router.navigateByUrl(this.router.url, { onSameUrlNavigation: 'reload' });
    }
}
