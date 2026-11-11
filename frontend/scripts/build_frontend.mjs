import { cp, mkdir, readFile, rm, writeFile } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const raw = (process.env.INVOICECLEAR_API_BASE_URL || '').trim();
const deployed = process.argv.includes('--deploy') || process.env.VERCEL === '1';
const invalidOrigin = () => new Error('INVOICECLEAR_API_BASE_URL must be an exact http(s) origin without credentials, whitespace, wildcard or a path.');
if (deployed && !raw) throw new Error('Set INVOICECLEAR_API_BASE_URL to the Railway HTTPS origin before deploying the frontend.');
let apiBaseUrl = '';
if (raw) {
  let url;
  try { url = new URL(raw); } catch { throw invalidOrigin(); }
  if (/[\s*<>"'`]/.test(raw) || !['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.search || url.hash || url.pathname !== '/') {
    throw invalidOrigin();
  }
  const loopback = url.hostname === 'localhost' || url.hostname.endsWith('.localhost') || url.hostname === '[::1]' || /^127\./.test(url.hostname);
  if (deployed && (url.protocol !== 'https:' || loopback)) throw new Error('A deployed frontend needs a non-loopback HTTPS backend origin.');
  apiBaseUrl = url.origin;
}
const index = await readFile(join(root, 'web', 'index.html'), 'utf8');
if (!index.includes('<head>')) throw new Error('The frontend document is missing its head element.');
const output = join(root, 'dist');
await rm(output, { recursive: true, force: true });
await mkdir(output, { recursive: true });
await cp(join(root, 'web'), output, { recursive: true });
// Intersects the hosting policy: browser API calls may reach only this backend.
const policy = `connect-src 'self'${apiBaseUrl ? ' ' + apiBaseUrl : ''}`;
await writeFile(join(output, 'index.html'), index.replace('<head>', `<head>\n  <meta http-equiv="Content-Security-Policy" content="${policy}">`));
await writeFile(join(output, 'config.js'), `window.__INVOICECLEAR_CONFIG__ = Object.freeze(${JSON.stringify({ apiBaseUrl })});\n`);
process.stdout.write('InvoiceClear frontend built.\n');
