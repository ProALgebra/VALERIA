import { ChangeDetectionStrategy, Component, computed, DestroyRef, inject, signal } from '@angular/core';
import { toSignal } from '@angular/core/rxjs-interop';
import { FormControl, FormGroup, ReactiveFormsModule } from '@angular/forms';
import { Router, RouterLink } from '@angular/router';
import { KbqAlertColors, KbqAlertModule } from '@koobiq/components/alert';
import { KbqButtonModule } from '@koobiq/components/button';
import { KbqComponentColors, PopUpPlacements } from '@koobiq/components/core';
import { KbqFormFieldModule } from '@koobiq/components/form-field';
import { FieldErrorDirective } from '../../../shared/field-error/field-error.directive';
import { KbqInputModule } from '@koobiq/components/input';
import { KbqRadioModule } from '@koobiq/components/radio';
import { KbqToolTipModule } from '@koobiq/components/tooltip';
import { ApiError, isAccessChanged } from '../../../core/api/api-error';
import { ApiService } from '../../../core/api/api.service';
import { DirtyAware, DirtyFormService } from '../../../core/guards/dirty.guard';
import { NotifyService } from '../../../core/notify/notify.service';
import { describeError } from '../../../core/status/error-codes';
import { ScreenHeaderComponent } from '../../../shell/screen-header/screen-header.component';
import { ACCOUNT_TYPES, AccountType, accountTypeToApi, AdminUser } from '../shared/admin.models';
import { FormPageComponent, FrowComponent, FsecComponent } from '../config-shared/form-layout';
import { ADMIN_FORM_STYLES } from '../shared/admin-styles';
import { SyncOp, SyncSlowAlertComponent } from '../shared/sync-op';
import { KbqIconModule } from '@koobiq/components/icon';

const MIN_PASSWORD = 12;

/** «Новый пользователь» (`/admin/users/new`, SPEC §10.2). */
@Component({
    selector: 'v-admin-user-create',
    imports: [
        FormPageComponent,
        FsecComponent,
        FrowComponent,
        ReactiveFormsModule,
        RouterLink,
        ScreenHeaderComponent,
        SyncSlowAlertComponent,
        KbqAlertModule,
        KbqButtonModule,
        KbqFormFieldModule,
        FieldErrorDirective,
        KbqInputModule,
        KbqRadioModule,
        KbqToolTipModule,
        KbqIconModule,
    ],
    changeDetection: ChangeDetectionStrategy.OnPush,
    template: `
        <v-screen-header title="Новый пользователь" />

        <form class="v-formwrap" [formGroup]="form" (ngSubmit)="create()" novalidate>
            <v-form-page>
                <v-sync-slow-alert [op]="op" />
                @if (saveError(); as e) {
                    <kbq-alert [compact]="true" [alertColor]="alertColors.Error" alertStyle="colored">
                        <i kbq-icon="kbq-circle-exclamation_16" color="error"></i>
                        <div kbq-alert-title>Не удалось создать пользователя</div>
                        {{ e }}
                    </kbq-alert>
                }

                <fieldset vFsec="Вход">
                    <v-frow label="Логин" for="v-login" [required]="true">
                        <kbq-form-field class="v-w320">
                            <input
                                kbqInput
                                id="v-login"
                                formControlName="login"
                                [vFieldError]="fieldError('login')"
                                autocomplete="off"
                                [required]="true"
                                [attr.aria-invalid]="!!fieldError('login')"
                            />
                            <kbq-error role="alert">{{ fieldError('login') }}</kbq-error>
                        </kbq-form-field>
                    </v-frow>
                    <v-frow label="Временный пароль" for="v-pass" [required]="true">
                        <kbq-form-field class="v-w320">
                            <input
                                kbqInput
                                type="text"
                                spellcheck="false"
                                id="v-pass"
                                formControlName="password"
                                [vFieldError]="fieldError('password')"
                                placeholder="Не менее 12 символов"
                                autocomplete="new-password"
                                [required]="true"
                                aria-describedby="v-pass-hint"
                            />
                            <kbq-error role="alert">{{ fieldError('password') }}</kbq-error>
                            <kbq-hint id="v-pass-hint">
                                VALERIA его никуда не отправляет, передайте пароль пользователю сами.
                            </kbq-hint>
                        </kbq-form-field>
                    </v-frow>
                    <v-frow label="Тип учётки" labelId="v-type-label">
                        <kbq-radio-group
                            formControlName="type"
                            aria-labelledby="v-type-label"
                        >
                            @for (t of types; track t.id) {
                                <kbq-radio-button [value]="t.id">
                                    {{ t.label }}
                                    @if (t.hint) {
                                        <kbq-hint>{{ t.hint }}</kbq-hint>
                                    }
                                </kbq-radio-button>
                            }
                        </kbq-radio-group>
                    </v-frow>
                </fieldset>

                <fieldset vFsec="Работа">
                    <v-frow label="Рабочая папка" for="v-ws">
                        <kbq-form-field>
                            <input
                                kbqInput
                                kbqInputMonospace
                                id="v-ws"
                                formControlName="ws"
                                [vFieldError]="fieldError('ws')"
                                placeholder="team/project"
                                aria-describedby="v-ws-hint"
                            />
                            <kbq-error role="alert">{{ fieldError('ws') }}</kbq-error>
                            <kbq-hint id="v-ws-hint">Путь внутри каталога данных сервера. Если папки нет, она создастся</kbq-hint>
                        </kbq-form-field>
                    </v-frow>
                    <v-frow label="Mattermost" for="v-mm">
                        <kbq-form-field class="v-w320">
                            <input
                                kbqInput
                                id="v-mm"
                                formControlName="mm"
                                [vFieldError]="fieldError('mm')"
                                placeholder="Логин без @"
                                aria-describedby="v-mm-hint"
                            />
                            <kbq-error role="alert">{{ fieldError('mm') }}</kbq-error>
                            <kbq-hint id="v-mm-hint">
                                Сюда Valeria пишет о готовых ответах и задачах. Можно указать позже.
                            </kbq-hint>
                        </kbq-form-field>
                    </v-frow>
                </fieldset>

                <ng-container vFootMain>
                    <span
                        [kbqTooltip]="'Укажите логин и пароль не короче 12 символов'"
                        [kbqTooltipDisabled]="ready()"
                        [kbqPlacement]="placements.Top"
                    >
                        <button
                            kbq-button
                            type="submit"
                            [color]="colors.Contrast"
                            [disabled]="!ready() || op.busy()"
                            [class.kbq-progress]="op.busy()"
                        >
                            Создать
                        </button>
                    </span>
                    <a kbq-button routerLink="/admin/users">Отмена</a>
                </ng-container>
            </v-form-page>
        </form>
    `,
    styles: [ADMIN_FORM_STYLES],
})
export class UserCreateComponent implements DirtyAware {
    private readonly api = inject(ApiService);
    private readonly router = inject(Router);
    private readonly notify = inject(NotifyService);

    protected readonly colors = KbqComponentColors;
    protected readonly alertColors = KbqAlertColors;
    protected readonly placements = PopUpPlacements;
    protected readonly types = ACCOUNT_TYPES;
    protected readonly op = new SyncOp();

    protected readonly form = new FormGroup({
        login: new FormControl('', { nonNullable: true }),
        password: new FormControl('', { nonNullable: true }),
        type: new FormControl<AccountType>('user', { nonNullable: true }),
        ws: new FormControl('', { nonNullable: true }),
        mm: new FormControl('', { nonNullable: true }),
    });
    private readonly value = toSignal(this.form.valueChanges, { initialValue: this.form.getRawValue() });
    protected readonly ready = computed(
        () => !!this.value().login?.trim() && (this.value().password ?? '').length >= MIN_PASSWORD,
    );
    protected readonly saveError = signal<string | null>(null);
    private readonly serverErrors = signal<Partial<Record<'login' | 'password' | 'ws' | 'mm', string>>>({});
    private created = false;
    /** Known logins: «Логин … уже занят» is shown before the backend asks for the workspace. */
    private knownLogins = new Set<string>();

    constructor() {
        this.api.get<AdminUser[]>('/api/admin/users').then(
            (users) => (this.knownLogins = new Set(users.map((u) => u.login))),
            () => undefined,
        );
        const untrack = inject(DirtyFormService).track(() => this.isDirty());
        inject(DestroyRef).onDestroy(untrack);
        for (const key of ['login', 'password', 'ws', 'mm'] as const) {
            this.form.controls[key].valueChanges.subscribe(() => this.clearField(key));
        }
    }

    isDirty(): boolean {
        if (this.created) return false;
        const v = this.form.getRawValue();
        return !!(v.login.trim() || v.password || v.ws.trim() || v.mm.trim() || v.type !== 'user');
    }

    protected fieldError(field: 'login' | 'password' | 'ws' | 'mm'): string | null {
        return this.serverErrors()[field] ?? null;
    }

    private clearField(field: 'login' | 'password' | 'ws' | 'mm'): void {
        if (!this.serverErrors()[field]) return;
        const next = { ...this.serverErrors() };
        delete next[field];
        this.serverErrors.set(next);
    }

    protected async create(): Promise<void> {
        if (!this.ready() || this.op.busy()) return;
        this.saveError.set(null);
        const v = this.form.getRawValue();
        const login = v.login.trim();
        const ws = v.ws.trim();
        const mm = v.mm.trim().replace(/^@/, '');
        if (this.knownLogins.has(login)) {
            this.serverErrors.set({ login: `Логин ${login} уже занят` });
            return;
        }
        if (v.type !== 'admin' && !ws) {
            this.serverErrors.set({ ws: 'Укажите рабочую папку' });
            return;
        }
        this.serverErrors.set({});
        const body: Record<string, unknown> = { login, password: v.password, ...accountTypeToApi(v.type) };
        if (ws) body['workspace_path'] = ws;
        if (mm) body['mattermost_username'] = mm;

        const result = await this.op.run(() => this.api.post<AdminUser>('/api/admin/users', body));
        if (result.ok) {
            this.created = true;
            await this.router.navigate(['/admin/users'], { queryParams: { focus: result.value.login ?? login } });
            this.notify.success('Пользователь создан', `${login} войдёт с временным паролем`);
            return;
        }
        if ('error' in result) await this.showError(result.error, login, mm);
    }

    private async showError(err: unknown, login: string, mm: string): Promise<void> {
        if (err instanceof ApiError && err.isConflict) {
            // The backend says «Login or Mattermost user already exists»: find out which one from the user list.
            let users: AdminUser[] = [];
            try {
                users = await this.api.get<AdminUser[]>('/api/admin/users');
            } catch {
                // Fall back to the login message.
            }
            const loginTaken = users.some((u) => u.login === login);
            const mmTaken = !!mm && users.some((u) => (u.mattermost_username ?? '').toLowerCase() === mm.toLowerCase());
            if (!loginTaken && mmTaken) {
                this.serverErrors.set({ mm: `Mattermost @${mm} уже привязан к другому пользователю` });
            } else {
                this.serverErrors.set({ login: `Логин ${login} уже занят` });
            }
            return;
        }
        if (err instanceof ApiError && err.status === 422 && err.detail === 'Workspace is required for a user') {
            this.serverErrors.set({ ws: 'Укажите рабочую папку' });
            return;
        }
        if (!isAccessChanged(err)) this.saveError.set(describeError(err).text);
    }
}
