/**
 * XHS Sign Wrapper — Node.js bridge for subprocess-based signing.
 * Usage: node sign_wrapper.js <api> <method>
 * Reads JSON from stdin: {"a1": "...", "data": {...}}
 * Writes JSON to stdout: {"xs": "...", "xt": ..., "xs_common": "..."}
 */
const path = require('path');

// Suppress vendor library init noise (prints '[Error]' to stdout on load)
const _origLog = console.log;
console.log = () => {};
const { get_request_headers_params } = require(path.join(process.cwd(), 'static', 'xhs_main_260411.js'));
console.log = _origLog;

const api = process.argv[2];
const method = process.argv[3] || 'POST';
let stdinData = '';

process.stdin.setEncoding('utf-8');
process.stdin.on('data', (chunk) => { stdinData += chunk; });
process.stdin.on('end', () => {
    try {
        const input = JSON.parse(stdinData);
        const a1 = input.a1 || '';
        const data = input.data || '';
        const result = get_request_headers_params(api, data, a1, method);
        console.log(JSON.stringify({ xs: result.xs, xt: result.xt, xs_common: result.xs_common }));
    } catch (e) {
        console.error(JSON.stringify({ error: e.message }));
        process.exit(1);
    }
});