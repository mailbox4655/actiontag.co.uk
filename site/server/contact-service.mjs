import { randomUUID } from 'node:crypto';
import http from 'node:http';
import { pathToFileURL } from 'node:url';
import {
  deliverContactMessage,
  loadContactConfiguration,
  parseContactSubmission,
} from './contact-core.mjs';

const BODY_LIMIT = 16 * 1024;
const RATE_WINDOW_MS = 10 * 60 * 1000;
const RATE_LIMIT = 5;
const MAX_RATE_KEYS = 10_000;

function json(response, status, body, extraHeaders = {}) {
  const data = Buffer.from(JSON.stringify(body));
  response.writeHead(status, {
    'Cache-Control': 'no-store',
    'Content-Type': 'application/json; charset=utf-8',
    'Content-Length': data.length,
    'X-Content-Type-Options': 'nosniff',
    ...extraHeaders,
  });
  response.end(data);
}

async function readBody(request) {
  const chunks = [];
  let length = 0;
  for await (const chunk of request) {
    length += chunk.length;
    if (length > BODY_LIMIT) throw Object.assign(new Error('request body is too large'), { status: 413 });
    chunks.push(chunk);
  }
  return Buffer.concat(chunks).toString('utf8');
}

function requestHost(request) {
  return String(request.headers.host ?? '').split(':', 1)[0].toLowerCase();
}

function requestAddress(request) {
  const cloudflare = String(request.headers['cf-connecting-ip'] ?? '').trim();
  if (cloudflare) return cloudflare.slice(0, 128);
  const forwarded = String(request.headers['x-forwarded-for'] ?? '').split(',')[0].trim();
  return (forwarded || request.socket.remoteAddress || 'unknown').slice(0, 128);
}

function makeRateLimiter(now) {
  const buckets = new Map();
  return (key) => {
    const timestamp = now();
    if (!buckets.has(key) && buckets.size >= MAX_RATE_KEYS) {
      buckets.delete(buckets.keys().next().value);
    }
    const existing = (buckets.get(key) ?? []).filter((item) => timestamp - item < RATE_WINDOW_MS);
    if (existing.length >= RATE_LIMIT) {
      buckets.set(key, existing);
      return false;
    }
    existing.push(timestamp);
    buckets.set(key, existing);
    return true;
  };
}

export function createActionTagServer({
  env = process.env,
  fetchImpl = globalThis.fetch,
  logger = console,
  now = Date.now,
} = {}) {
  const configuration = loadContactConfiguration(env);
  const allowRequest = makeRateLimiter(now);

  const server = http.createServer(async (request, response) => {
    const url = new URL(request.url ?? '/', 'http://actiontag.invalid');

    if (request.method === 'GET' && url.pathname === '/healthz') {
      json(response, 200, {
        status: 'healthy',
        release: configuration.release,
        mail: 'configured',
      });
      return;
    }

    if (request.method !== 'POST' || url.pathname !== '/api/contact') {
      json(response, 404, { error: 'not found' });
      return;
    }

    const host = requestHost(request);
    if (!configuration.trustedHosts.has(host)) {
      json(response, 403, { error: 'host not allowed' });
      return;
    }
    const origin = String(request.headers.origin ?? '').trim();
    if (origin) {
      try {
        const parsedOrigin = new URL(origin);
        if (parsedOrigin.protocol !== 'https:' ||
            !configuration.trustedHosts.has(parsedOrigin.hostname.toLowerCase())) {
          json(response, 403, { error: 'origin not allowed' });
          return;
        }
      } catch {
        json(response, 403, { error: 'origin not allowed' });
        return;
      }
    }

    const contentType = String(request.headers['content-type'] ?? '').toLowerCase();
    if (!contentType.startsWith('application/x-www-form-urlencoded')) {
      json(response, 415, { error: 'unsupported media type' });
      return;
    }
    if (!allowRequest(requestAddress(request))) {
      json(response, 429, { error: 'rate limited' }, { 'Retry-After': '600' });
      return;
    }

    const correlationId = randomUUID();
    try {
      const submission = parseContactSubmission(new URLSearchParams(await readBody(request)));
      if (submission.ignored) {
        logger.info(`contact ignored correlation=${correlationId}`);
        json(response, 200, { ok: true });
        return;
      }
      if (submission.error) {
        json(response, 422, { error: submission.error });
        return;
      }

      const delivered = await deliverContactMessage({
        submission,
        configuration,
        correlationId,
        fetchImpl,
      });
      logger.info(`contact delivered correlation=${correlationId} postmark=${delivered.messageId}`);
      json(response, 200, { ok: true, correlationId });
    } catch (error) {
      const status = Number.isInteger(error?.status) ? error.status : 502;
      logger.error(`contact failed correlation=${correlationId} reason=${error?.message ?? 'unknown error'}`);
      json(response, status, { error: status === 413 ? 'request too large' : 'send failed', correlationId });
    }
  });

  return { server, configuration };
}

async function main() {
  const port = Number.parseInt(process.env.ACTIONTAG_PORT ?? '3021', 10);
  const bindHost = String(process.env.ACTIONTAG_BIND_HOST ?? '').trim();
  if (!Number.isInteger(port) || port < 1 || port > 65535) {
    throw new Error('ACTIONTAG_PORT must be an integer from 1 through 65535');
  }
  if (bindHost !== '127.0.0.1') {
    throw new Error('ACTIONTAG_BIND_HOST must be exactly 127.0.0.1');
  }
  const { server, configuration } = createActionTagServer();
  server.listen(port, bindHost, () => {
    console.info(`ActionTag contact service listening on ${bindHost}:${port} release=${configuration.release}`);
  });
  for (const signal of ['SIGINT', 'SIGTERM']) {
    process.once(signal, () => server.close(() => process.exit(0)));
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((error) => {
    console.error(`ActionTag contact service startup failed: ${error.message}`);
    process.exit(1);
  });
}
