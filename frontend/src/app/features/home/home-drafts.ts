import { inject, Injectable } from '@angular/core';
import { UiStateService } from '../../core/state/ui-state.service';

/** «Я только спросить» (question) or «Сделать» (folder work). */
export type HomeMode = 'ask' | 'do';

export const HOME_DRAFT_ID = 'home';
export const HOME_MODE_ID = 'home.mode';
export const taskDraftId = (taskId: string | number): string => `task.${taskId}`;

/**
 * Drafts of the home screen in sessionStorage via UiStateService (SPEC §6.4): the text of «Что нужно сделать?»
 * (one for both modes, like the prototype), the chosen mode and the addition of each task.
 * They survive a tab reload and are cleared after a successful send.
 */
@Injectable({ providedIn: 'root' })
export class HomeDrafts {
    private readonly ui = inject(UiStateService);

    text(): string {
        return this.ui.readDraft(HOME_DRAFT_ID);
    }

    setText(text: string): void {
        this.ui.writeDraft(HOME_DRAFT_ID, text);
    }

    mode(): HomeMode {
        return this.ui.readDraft(HOME_MODE_ID) === 'do' ? 'do' : 'ask';
    }

    setMode(mode: HomeMode): void {
        this.ui.writeDraft(HOME_MODE_ID, mode === 'do' ? 'do' : '');
    }

    addition(taskId: string | number): string {
        return this.ui.readDraft(taskDraftId(taskId));
    }

    setAddition(taskId: string | number, text: string): void {
        this.ui.writeDraft(taskDraftId(taskId), text);
    }

    /** Opens home in a given mode with a given text, e.g. from the search palette or a template. */
    prepare(mode: HomeMode, text: string): void {
        this.setMode(mode);
        this.setText(text);
    }
}
