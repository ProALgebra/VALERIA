/** Agent question as returned by `GET …/questions` (OpenCode `question.asked`). */
export interface QuestionOption {
    label: string;
    description?: string;
}

export interface QuestionInfo {
    question: string;
    header?: string;
    multiple?: boolean;
    /** Free-text answer allowed («Свой ответ»). OpenCode treats a missing flag as allowed. */
    custom?: boolean;
    options: QuestionOption[];
}

export interface QuestionRequest {
    id: string;
    questions: QuestionInfo[];
}

/** What the user picked for one question. */
export interface QuestionSelection {
    /** Chosen option labels. */
    selected: string[];
    /** «Свой ответ» is on. */
    customOn: boolean;
    customText: string;
}

export const emptySelection = (): QuestionSelection => ({ selected: [], customOn: false, customText: '' });

export function allowsCustom(q: QuestionInfo): boolean {
    return q.custom !== false;
}

/** Values sent for one question, or null when the question is not answered yet. */
export function answerFor(q: QuestionInfo, s: QuestionSelection): string[] | null {
    const custom = allowsCustom(q) && s.customOn ? s.customText.trim() : '';
    const labels = q.options.map((o) => o.label);
    const picked = s.selected.filter((label) => labels.includes(label));
    if (!q.multiple) {
        // Radio: «Свой ответ» is an alternative to the options.
        if (allowsCustom(q) && s.customOn) return custom ? [custom] : null;
        return picked.length ? [picked[0]] : null;
    }
    if (allowsCustom(q) && s.customOn && !custom) return null;
    const values = [...labels.filter((l) => picked.includes(l)), ...(custom ? [custom] : [])];
    return values.length ? values : null;
}

/** Body of `POST …/questions/:id/reply`: one list of values per question, in order; null while incomplete. */
export function buildReplyPayload(request: QuestionRequest, selections: QuestionSelection[]): { answers: string[][] } | null {
    const answers: string[][] = [];
    for (let i = 0; i < request.questions.length; i++) {
        const answer = answerFor(request.questions[i], selections[i] ?? emptySelection());
        if (!answer) return null;
        answers.push(answer);
    }
    return { answers };
}
