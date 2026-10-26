import { ChangeDetectionStrategy, Component, input } from '@angular/core';
import { KbqComponentColors } from '@koobiq/components/core';
import { KbqIconModule } from '@koobiq/components/icon';
import { AgentStep, StepIcon } from './steps';

const ICONS: Record<StepIcon, string> = {
    read: 'kbq-file-o_16',
    search: 'kbq-magnifying-glass_16',
    edit: 'kbq-pencil_16',
    run: 'kbq-bolt_16',
    tool: 'kbq-gear_16',
};

/** Agent steps under a message (prototype `.v-step`): fade icon, text, secondary sub. */
@Component({
    selector: 'v-agent-steps',
    imports: [KbqIconModule],
    changeDetection: ChangeDetectionStrategy.OnPush,
    template: `
        @for (step of steps(); track step.id) {
            <div class="v-step kbq-text-compact" [class.v-step_failed]="step.failed">
                <i
                    kbq-icon
                    [class]="icons[step.icon]"
                    [color]="step.failed ? colors.Error : colors.ContrastFade"
                    aria-hidden="true"
                ></i>
                <span class="v-step__text">{{ step.text }}</span>
                @if (step.sub) {
                    <span class="v-step__sub">{{ step.sub }}</span>
                }
            </div>
        }
    `,
    styles: `
        :host {
            /* Two columns as in the prototype: step text, then the secondary detail; details line up across rows. */
            display: grid;
            grid-template-columns: auto minmax(0, max-content) minmax(0, 1fr);
        }
        .v-step {
            display: grid;
            grid-column: 1 / -1;
            grid-template-columns: subgrid;
            align-items: center;
            gap: var(--kbq-size-s);
            padding: var(--kbq-size-xs) var(--kbq-size-s);
            margin: var(--kbq-size-3xs) 0;
            border-radius: var(--kbq-size-xs);
        }
        .v-step:hover { background: var(--kbq-states-background-transparent-hover); }
        .v-step i { flex: none; }
        .v-step__text { min-width: 0; overflow-wrap: anywhere; }
        .v-step__sub {
            min-width: 0;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
            color: var(--kbq-foreground-contrast-secondary);
        }
        .v-step_failed .v-step__sub { color: var(--kbq-foreground-error); }
    `,
})
export class AgentStepsComponent {
    readonly steps = input.required<AgentStep[]>();
    protected readonly icons = ICONS;
    protected readonly colors = KbqComponentColors;
}
