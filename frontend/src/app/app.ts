import { ChangeDetectionStrategy, Component } from '@angular/core';
import { RouterOutlet } from '@angular/router';
import { NotifyHostComponent } from './core/notify/notify-host.component';

@Component({
    selector: 'v-root',
    imports: [RouterOutlet, NotifyHostComponent],
    changeDetection: ChangeDetectionStrategy.OnPush,
    template: `<router-outlet /><v-notify-host />`,
})
export class App {}
