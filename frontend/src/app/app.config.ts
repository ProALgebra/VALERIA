import { registerLocaleData } from '@angular/common';
import { provideHttpClient, withInterceptors } from '@angular/common/http';
import localeRu from '@angular/common/locales/ru';
import {
    ApplicationConfig,
    inject,
    LOCALE_ID,
    provideAppInitializer,
    provideBrowserGlobalErrorListeners,
    provideZoneChangeDetection,
} from '@angular/core';
import { provideAnimations } from '@angular/platform-browser/animations';
import { provideRouter, withComponentInputBinding } from '@angular/router';
import { LuxonDateAdapter } from '@koobiq/angular-luxon-adapter/adapter';
import { DateAdapter, DateFormatter, KBQ_DATE_LOCALE, KBQ_LOCALE_SERVICE, KbqLocaleService } from '@koobiq/components/core';
import { kbqToastConfigurationProvider, KbqToastPosition } from '@koobiq/components/toast';
import { routes } from './app.routes';
import { apiInterceptor, ON_FORBIDDEN, ON_UNAUTHORIZED } from './core/api/api.service';
import { AuthService } from './core/auth/auth.service';
import { AppThemeService } from './core/theme/theme.service';

registerLocaleData(localeRu, 'ru');

export const appConfig: ApplicationConfig = {
    providers: [
        provideBrowserGlobalErrorListeners(),
        provideZoneChangeDetection({ eventCoalescing: true }),
        provideRouter(routes, withComponentInputBinding()),
        provideHttpClient(withInterceptors([apiInterceptor])),
        // Koobiq dropdown, modal and content panel still use @angular/animations.
        provideAnimations(),
        { provide: LOCALE_ID, useValue: 'ru' },
        { provide: KBQ_LOCALE_SERVICE, useClass: KbqLocaleService },
        // The only date locale provider. The Luxon adapter needs a full locale name ('ru' throws on creation).
        { provide: KBQ_DATE_LOCALE, useValue: 'ru-RU' },
        { provide: DateAdapter, useClass: LuxonDateAdapter },
        DateFormatter,
        kbqToastConfigurationProvider({ position: KbqToastPosition.BOTTOM_RIGHT, indent: { vertical: 16, horizontal: 16 } }),
        {
            provide: ON_UNAUTHORIZED,
            useFactory: () => {
                const auth = inject(AuthService);
                return () => auth.reset();
            },
        },
        {
            provide: ON_FORBIDDEN,
            useFactory: () => {
                const auth = inject(AuthService);
                return () => auth.handleForbidden();
            },
        },
        provideAppInitializer(() => inject(AppThemeService).init()),
    ],
};
