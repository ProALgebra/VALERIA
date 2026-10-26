// Dev proxy: /api (and the mock's /__mock controls) → VALERIA_API (default: the mock server on 4301).
// SSE (/api/events) must stream, so compression and buffering are switched off.
const target = process.env.VALERIA_API || 'http://127.0.0.1:4301';

export default {
    '/api': {
        target,
        secure: true,
        changeOrigin: true,
        ws: false,
        configure: (proxy) => {
            proxy.on('proxyReq', (proxyReq, req) => {
                if (req.url && req.url.startsWith('/api/events')) {
                    proxyReq.setHeader('accept-encoding', 'identity');
                    proxyReq.setHeader('cache-control', 'no-cache');
                }
            });
            proxy.on('proxyRes', (proxyRes, req) => {
                const type = String(proxyRes.headers['content-type'] || '');
                if (type.includes('text/event-stream')) {
                    proxyRes.headers['cache-control'] = 'no-cache, no-transform';
                    proxyRes.headers['x-accel-buffering'] = 'no';
                    delete proxyRes.headers['content-length'];
                }
            });
        },
    },
    // Mock-only controls (persona switch, reset); harmless against the real backend (404).
    '/__mock': { target, secure: true, changeOrigin: true },
};
