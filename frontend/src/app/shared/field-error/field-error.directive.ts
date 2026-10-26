import { Directive, inject, input } from '@angular/core';
import { ErrorStateMatcher } from '@koobiq/components/core';
import { KbqFormFieldControl } from '@koobiq/components/form-field';

/**
 * Puts a Koobiq form-field control into the error state while it has a field error (client check or a
 * server `field_errors` entry), so the field gets the red border and its `<kbq-error>` slot renders.
 * Koobiq shows `kbq-error` only for an invalid control; our errors don't live in Angular validators.
 * Works for kbqInput, kbqInputPassword and kbqTextarea (number inputs are not form-field controls).
 */
@Directive({ selector: '[vFieldError]' })
export class FieldErrorDirective {
    readonly vFieldError = input<string | null | undefined>(null);

    constructor() {
        const control = inject(KbqFormFieldControl, { self: true }) as KbqFormFieldControl<unknown> & {
            errorStateMatcher?: ErrorStateMatcher;
        };
        control.errorStateMatcher = { isErrorState: () => !!this.vFieldError() };
    }
}
