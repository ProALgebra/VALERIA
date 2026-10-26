import {
    afterNextRender,
    ChangeDetectionStrategy,
    Component,
    ElementRef,
    inject,
    input,
    signal,
    viewChild,
} from '@angular/core';
import { FormControl, FormGroup, ReactiveFormsModule, Validators } from '@angular/forms';
import { Router } from '@angular/router';
import { KbqButtonModule } from '@koobiq/components/button';
import { KbqComponentColors } from '@koobiq/components/core';
import { KbqFormFieldModule } from '@koobiq/components/form-field';
import { KbqInputModule } from '@koobiq/components/input';
import { toApiError } from '../../core/api/api-error';
import { AuthService } from '../../core/auth/auth.service';
import { safeReturnUrl } from '../../core/guards/auth.guard';
import { describeError } from '../../core/status/error-codes';

/** Text of the backend 401 for a wrong pair (app.py), used when the response has no detail. */
export const WRONG_CREDENTIALS = 'Неверный логин или пароль';

/** `/login` (SPEC §5). After sign-in goes to `?return=` (same-origin paths only) or home. */
@Component({
    selector: 'v-login',
    imports: [ReactiveFormsModule, KbqFormFieldModule, KbqInputModule, KbqButtonModule],
    changeDetection: ChangeDetectionStrategy.OnPush,
    template: `
        <form class="v-login" [formGroup]="form" (ngSubmit)="submit()" novalidate>
            <div class="v-login__brand" aria-hidden="true">
                <span class="v-login__logo"></span>
                <span>VALERIA</span>
            </div>
            <h1 class="kbq-title v-login__title">Вход в систему</h1>

            <kbq-form-field>
                <kbq-label>Логин</kbq-label>
                <input #loginField kbqInput formControlName="login" autocomplete="username" />
            </kbq-form-field>

            <kbq-form-field>
                <kbq-label>Пароль</kbq-label>
                <input kbqInput type="password" formControlName="password" autocomplete="current-password" />
            </kbq-form-field>

            @if (error(); as message) {
                <div class="v-login__error kbq-text-normal" role="alert">{{ message }}</div>
            }

            <button kbq-button type="submit" class="v-login__submit" [color]="colors.Contrast" [disabled]="pending()">
                Войти
            </button>
            <p class="v-login__hint kbq-text-compact">Доступ предоставляется администратором</p>
        </form>
    `,
    styles: `
        :host {
            display: flex;
            align-items: center;
            justify-content: center;
            min-height: 100%;
            padding: var(--kbq-size-xxl);
            box-sizing: border-box;
            background: var(--kbq-background-bg-secondary);
        }
        .v-login {
            display: flex;
            flex-direction: column;
            gap: var(--kbq-size-l);
            width: 360px;
            max-width: 100%;
            padding: var(--kbq-size-xxl);
            border: var(--kbq-size-border-width) solid var(--kbq-line-contrast-less);
            border-radius: var(--kbq-size-m);
            background: var(--kbq-background-bg);
            box-sizing: border-box;
        }
        .v-login__brand { display: flex; align-items: center; gap: var(--kbq-size-s); font-weight: var(--kbq-typography-text-normal-strong-font-weight); color: var(--kbq-foreground-contrast); }
        .v-login__logo { width: var(--kbq-size-xxl); height: var(--kbq-size-xxl); border-radius: var(--kbq-size-xs); background: var(--kbq-background-theme); }
        .v-login__title { margin: 0; color: var(--kbq-foreground-contrast); }
        .v-login__error { color: var(--kbq-foreground-error); }
        .v-login__submit { width: 100%; }
        .v-login__hint { margin: 0; text-align: center; color: var(--kbq-foreground-contrast-secondary); }
    `,
})
export class LoginComponent {
    private readonly auth = inject(AuthService);
    private readonly router = inject(Router);

    /** `?return=` query parameter (bound by the router). */
    readonly returnUrl = input<string | undefined>(undefined, { alias: 'return' });

    protected readonly colors = KbqComponentColors;
    protected readonly pending = signal(false);
    protected readonly error = signal<string | null>(null);
    protected readonly form = new FormGroup({
        login: new FormControl('', { nonNullable: true, validators: [Validators.required] }),
        password: new FormControl('', { nonNullable: true, validators: [Validators.required] }),
    });
    private readonly loginField = viewChild.required<ElementRef<HTMLInputElement>>('loginField');

    constructor() {
        // The `autofocus` attribute works on page load only, not when the router renders the screen.
        afterNextRender(() => this.loginField().nativeElement.focus());
    }

    async submit(): Promise<void> {
        if (this.pending()) return;
        if (this.form.invalid) {
            this.form.markAllAsTouched();
            return;
        }
        this.pending.set(true);
        this.error.set(null);
        const { login, password } = this.form.getRawValue();
        try {
            await this.auth.login(login, password);
            await this.router.navigateByUrl(safeReturnUrl(this.returnUrl()));
        } catch (e) {
            const err = toApiError(e);
            this.error.set(err.isUnauthorized ? (err.detail ?? WRONG_CREDENTIALS) : describeError(err).text);
        } finally {
            this.pending.set(false);
        }
    }
}
