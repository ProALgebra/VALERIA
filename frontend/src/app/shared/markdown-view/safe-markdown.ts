import { MarkedOptions, Renderer, Tokens } from 'marked';

/** Schemes we render as links. Hrefs without a scheme (relative, `/abs`, `?q`, `#anchor`) are links too; any other
 * scheme (javascript:, data:, vbscript:, ...) is rendered as plain text. */
const SAFE_SCHEMES = new Set(['http', 'https', 'mailto']);

export function escapeHtml(value: string): string {
    return value
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

let entityDecoder: HTMLTextAreaElement | null = null;

/**
 * Decodes HTML character references (`&colon;`, `&#58;`, `&#x3A;`, `&Tab;`, ...) the way the browser will when it
 * reads the href attribute. `marked` leaves them as typed, so checking the raw href lets `javascript&colon;` through.
 * The decoding runs in a `<textarea>` of an inert document: RCDATA, so only character references are interpreted.
 */
export function decodeEntities(value: string): string {
    if (!value.includes('&')) return value;
    entityDecoder ??= document.implementation.createHTMLDocument('').createElement('textarea');
    entityDecoder.innerHTML = value;
    return entityDecoder.value;
}

/** Href as the browser would resolve it, or null when it is not a web, mail or relative link. */
export function safeHref(href: string | null | undefined): string | null {
    if (!href) return null;
    const decoded = decodeEntities(href);
    // Browsers ignore control chars and whitespace inside the scheme (`java\tscript:`), so strip them before checking.
    // eslint-disable-next-line no-control-regex
    const compact = decoded.replace(/[\u0000-\u0020\u007f-\u009f\u00a0\u1680\u2000-\u200f\u2028\u2029\u202f\u205f\u3000\ufeff]/g, '');
    const firstDelimiter = compact.search(/[:/?#]/);
    if (firstDelimiter === -1 || compact[firstDelimiter] !== ':') return decoded; // no scheme: relative link
    return SAFE_SCHEMES.has(compact.slice(0, firstDelimiter).toLowerCase()) ? decoded : null;
}

export function isSafeHref(href: string | null | undefined): boolean {
    return safeHref(href) !== null;
}

/**
 * Renderer for agent markdown. `kbq-markdown` trusts whatever `marked` returns (`bypassSecurityTrustHtml`), so raw
 * HTML from the agent is shown as text, unsafe link schemes become plain text and images become their alt text.
 */
export function createSafeRenderer(): Renderer {
    const renderer = new Renderer();
    renderer.html = ({ text }: Tokens.HTML | Tokens.Tag) => escapeHtml(text);
    renderer.link = ({ href, title, tokens }: Tokens.Link) => {
        const text = renderer.parser.parseInline(tokens);
        const target = safeHref(href);
        if (target === null) return text;
        const titleAttr = title ? ` title="${escapeHtml(decodeEntities(title))}"` : '';
        return `<a href="${escapeHtml(target)}"${titleAttr}>${text}</a>`;
    };
    renderer.image = ({ text }: Tokens.Image) => escapeHtml(text);
    // After an inline <code>, <pre>, <kbd> or <script> tag marked's lexer is in raw-block mode and flags later text as
    // `escaped`, which the default renderer copies as is; text can start with `<` when it fails the tag regex.
    // Always escape (existing character references such as `&copy;` are kept).
    const defaultText = renderer.text.bind(renderer);
    renderer.text = (token: Tokens.Text | Tokens.Escape) => defaultText({ ...token, escaped: false } as typeof token);
    return renderer;
}

export const SAFE_MARKED_OPTIONS: MarkedOptions = { gfm: true, breaks: false, renderer: createSafeRenderer() };
