import { marked } from 'marked';
import { isSafeHref, SAFE_MARKED_OPTIONS } from './safe-markdown';

const render = (md: string) => marked.parse(md, { ...SAFE_MARKED_OPTIONS, async: false }) as string;

describe('safe markdown renderer', () => {
    it('escapes block HTML such as <script>', () => {
        const html = render('<script>alert(1)</script>');
        expect(html).not.toContain('<script');
        expect(html).toContain('&lt;script&gt;');
    });

    it('escapes inline HTML such as <img onerror>', () => {
        const html = render('text <img src=x onerror="alert(1)"> more');
        expect(html).not.toContain('<img');
        expect(html).toContain('&lt;img');
    });

    it('drops javascript: links but keeps their text', () => {
        const html = render('[click](javascript:alert(1))');
        expect(html).not.toContain('href');
        expect(html).toContain('click');
    });

    it('keeps https links', () => {
        expect(render('[docs](https://example.com)')).toContain('<a href="https://example.com">docs</a>');
    });

    it('renders images as their alt text', () => {
        const html = render('![logo](https://evil.example/x.png)');
        expect(html).not.toContain('<img');
        expect(html).toContain('logo');
    });

    it('renders regular markdown', () => {
        expect(render('Use `foo`')).toContain('<code>foo</code>');
    });

    it.each([
        ['https://a.b', true],
        ['/relative', true],
        ['page#x', true],
        ['mailto:a@b.c', true],
        ['javascript:alert(1)', false],
        ['JAVA\tSCRIPT:alert(1)', false],
        ['data:text/html,x', false],
        ['vbscript:x', false],
    ])('isSafeHref(%s) = %s', (href, safe) => {
        expect(isSafeHref(href)).toBe(safe);
    });
});

describe('safe markdown renderer: entity-encoded and obfuscated schemes', () => {
    const noJsLink = (html: string) => {
        expect(html).not.toMatch(/<a\b/i);
        expect(html).not.toMatch(/href/i);
    };

    it.each([
        ['[x](javascript&colon;alert(1))'],
        ['[x](javascript&#58;alert(1))'],
        ['[x](javascript&#x3A;alert(1))'],
        ['[x](javascript&#X3a;alert(1))'],
        ['[x](javascript&#0000058;alert(1))'],
        ['[x](JaVaScRiPt:alert(1))'],
        ['[x](&#106;avascript:alert(1))'],
        ['[x](java&Tab;script:alert(1))'],
        ['[x](java&#9;script:alert(1))'],
        ['[x](java&NewLine;script:alert(1))'],
        ['[x](<java\tscript:alert(1)>)'],
        ['[x](data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==)'],
        ['[x](data&colon;text/html,x)'],
        ['[x](vbscript:msgbox(1))'],
        ['[x](vbscript&colon;msgbox(1))'],
        ['[x][1]\n\n[1]: javascript&colon;alert(1)'],
        ['[x][1]\n\n[1]: javascript&#58;alert(1)'],
        ['[x](<javascript&colon;alert(1)>)'],
        ['<javascript&colon;alert(1)> x'],
    ])('does not render a link for %j', (md) => {
        const html = render(md);
        noJsLink(html);
        expect(html).toContain('x');
    });

    it('escapes raw HTML anchors with javascript: hrefs', () => {
        for (const md of ['<a href=javascript:alert(1)>x</a>', 'text <a href="javascript&colon;alert(1)">x</a>']) {
            const html = render(md);
            expect(html).not.toMatch(/<a\b/i);
            expect(html).toContain('&lt;a');
        }
    });

    it('escapes the href attribute of safe links', () => {
        const html = render('[x](https://a.b/?q="onmouseover=alert(1)")');
        expect(html).not.toMatch(/href="[^"]*"\s*onmouseover/i);
        expect(html).toMatch(/<a href="https:\/\/a\.b\/\?q=[^"]*">x<\/a>/);
    });

    it('keeps relative, anchor and query links', () => {
        expect(render('[x](/chats/1)')).toContain('<a href="/chats/1">x</a>');
        expect(render('[x](#top)')).toContain('<a href="#top">x</a>');
        expect(render('[x](page?a=1:2)')).toContain('<a href="page?a=1:2">x</a>');
        expect(render('[x](mailto:a@b.c)')).toContain('<a href="mailto:a@b.c">x</a>');
    });

    it.each([
        ['javascript&colon;alert(1)', false],
        ['javascript&#58;alert(1)', false],
        ['javascript&#x3A;alert(1)', false],
        ['javascript&#0000058;alert(1)', false],
        ['JaVaScRiPt:alert(1)', false],
        ['java\nscript:alert(1)', false],
        ['  javascript:alert(1)', false],
        ['data&colon;text/html,x', false],
        ['ftp://a.b', false],
        ['HTTPS://a.b', true],
        ['a/b:c', true],
        ['?x=a:b', true],
    ])('isSafeHref(%j) = %s', (href, safe) => {
        expect(isSafeHref(href)).toBe(safe);
    });
});

/**
 * An inline `<code>`, `<pre>`, `<kbd>` or `<script>` tag puts marked's lexer into raw-block mode: later text tokens are
 * marked `escaped` and the default text renderer copies them as is. A fragment that fails marked's tag regex then
 * arrives as text starting with `<`.
 */
describe('safe markdown renderer: text after an inline raw-block tag', () => {
    it.each([
        ['a <code> <img/src="x"/onerror=alert(1)>', '<img'],
        ['a <kbd> <img src=x onerror=alert(1)//', '<img'],
        ['a <pre> <a/href="//evil">x', '<a'],
    ])('%s stays text', (md, tag) => {
        const html = render(md);
        expect(html).not.toContain(tag);
        expect(html).toContain('&lt;');
    });

    it('keeps character references such as &copy; and &amp; as typed', () => {
        expect(render('&copy; 2026 &amp; <code> &copy;')).toContain('&copy; 2026 &amp; &lt;code&gt; &copy;');
    });
});

describe('safe markdown renderer: fuzz', () => {
    const ALLOWED_ELEMENTS = new Set([
        'p', 'a', 'code', 'pre', 'em', 'strong', 'del', 'br', 'hr', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
        'ul', 'ol', 'li', 'blockquote', 'table', 'thead', 'tbody', 'tr', 'th', 'td', 'input',
    ]);
    const ALLOWED_ATTRIBUTES = new Set(['href', 'title', 'align', 'start', 'type', 'checked', 'disabled', 'class']);
    const PIECES = [
        '<code>', '</code>', '<pre>', '</pre>', '<kbd>', '<script>', '</script>', '<img/src="x"/onerror=alert(1)>',
        '<img src=x onerror=alert(1)//', '<a/href="//evil">', '<a href="javascript:alert(1)">', '<svg onload=alert(1)>',
        '<iframe src=//evil>', '<b onmouseover=x>', '<details open ontoggle=x>', '<!-- c -->', '<', '>', '"', "'", '&',
        '&lt;', '&#60;', '&copy;', '`', '``', '*', '_', '**', '~~', '[x](javascript:alert(1))', '[y](https://a.b)',
        '![i](x.png)', '<https://a.b>', '\\<', '\n', '\n\n', '    ', '- ', '1. ', '> ', '# ', '| a | b |\n|---|---|\n',
        '```\n', 'text', ' ', '/', '=', 'onerror=alert(1)',
    ];

    /** Deterministic so a failure can be replayed. */
    function rng(seed: number) {
        return () => {
            seed = (seed * 1103515245 + 12345) & 0x7fffffff;
            return seed / 0x7fffffff;
        };
    }

    it('400 random mixes of tags and markdown render only allowed elements and safe attributes', () => {
        const next = rng(42);
        const parser = new DOMParser();
        for (let n = 0; n < 400; n++) {
            const length = 2 + Math.floor(next() * 12);
            const md = Array.from({ length }, () => PIECES[Math.floor(next() * PIECES.length)]).join('');
            const html = render(md);
            const doc = parser.parseFromString(`<body>${html}</body>`, 'text/html');
            for (const el of Array.from(doc.body.querySelectorAll('*'))) {
                const name = el.localName;
                expect(ALLOWED_ELEMENTS.has(name), `<${name}> from ${JSON.stringify(md)}`).toBe(true);
                for (const attr of Array.from(el.attributes)) {
                    const where = `${name}[${attr.name}] from ${JSON.stringify(md)}`;
                    expect(attr.name.startsWith('on'), where).toBe(false);
                    expect(ALLOWED_ATTRIBUTES.has(attr.name), where).toBe(true);
                    if (attr.name === 'href') expect(isSafeHref(attr.value), where).toBe(true);
                    if (name === 'input') expect(el.getAttribute('type'), where).toBe('checkbox');
                }
            }
        }
    });
});
