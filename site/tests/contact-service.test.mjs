import assert from 'node:assert/strict';
import http from 'node:http';
import test from 'node:test';
import {
  deliverContactMessage,
  loadContactConfiguration,
  parseContactSubmission,
} from '../server/contact-core.mjs';
import { createActionTagServer } from '../server/contact-service.mjs';

const environment = {
  ACTIONTAG_RELEASE: '0123456789ab',
  ACTIONTAG_TRUSTED_HOSTS: 'actiontag.co.uk,www.actiontag.co.uk,actiontag.redevelopingdeveloper.uk',
  POSTMARK_SERVER_TOKEN: 'test-token-never-used',
  POSTMARK_FROM_EMAIL: 'verified@example.test',
  POSTMARK_FROM_NAME: 'Verified sender',
  POSTMARK_MESSAGE_STREAM: 'outbound',
  CONTACT_TO: 'sales@actiontag.co.uk',
  CONTACT_CC: 'office@actiontag.co.uk',
};

function request(port, { method = 'GET', path = '/healthz', body = '', headers = {} } = {}) {
  return new Promise((resolve, reject) => {
    const req = http.request({
      host: '127.0.0.1',
      port,
      method,
      path,
      headers: { Host: 'actiontag.co.uk', ...headers },
    }, (response) => {
      const chunks = [];
      response.on('data', (chunk) => chunks.push(chunk));
      response.on('end', () => resolve({
        status: response.statusCode,
        headers: response.headers,
        body: JSON.parse(Buffer.concat(chunks).toString('utf8')),
      }));
    });
    req.on('error', reject);
    req.end(body);
  });
}

test('configuration fails loudly when a required secret is absent', () => {
  const incomplete = { ...environment };
  delete incomplete.POSTMARK_SERVER_TOKEN;
  assert.throws(() => loadContactConfiguration(incomplete), /POSTMARK_SERVER_TOKEN/);
});

test('contact parser preserves the approved fields and ignores the honeypot', () => {
  assert.deepEqual(parseContactSubmission(new URLSearchParams({ company_website: 'bot' })), { ignored: true });
  assert.deepEqual(parseContactSubmission(new URLSearchParams({
    name: 'Alex',
    email: 'alex@example.com',
    phone_cc: '+44',
    phone: '123456',
    message: 'Please contact me.',
  })), {
    name: 'Alex',
    email: 'alex@example.com',
    phone: '+44 123456',
    message: 'Please contact me.',
  });
});

test('Postmark payload sends to sales and copies office without exposing the token', async () => {
  const configuration = loadContactConfiguration(environment);
  let captured;
  const delivered = await deliverContactMessage({
    submission: { name: 'Alex', email: 'alex@example.com', phone: '', message: 'Hello' },
    configuration,
    correlationId: 'correlation-1',
    fetchImpl: async (url, options) => {
      captured = { url, options };
      return { ok: true, status: 200, json: async () => ({ MessageID: 'postmark-message-1' }) };
    },
  });
  const payload = JSON.parse(captured.options.body);
  assert.equal(captured.url, 'https://api.postmarkapp.com/email');
  assert.equal(payload.To, 'sales@actiontag.co.uk');
  assert.equal(payload.Cc, 'office@actiontag.co.uk');
  assert.equal(payload.ReplyTo, 'alex@example.com');
  assert.equal(captured.options.headers['X-Postmark-Server-Token'], environment.POSTMARK_SERVER_TOKEN);
  assert.ok(!captured.options.body.includes(environment.POSTMARK_SERVER_TOKEN));
  assert.deepEqual(delivered, { messageId: 'postmark-message-1' });
});

test('native service exposes release health and completes a valid contact request', async (t) => {
  const deliveries = [];
  const logger = { info() {}, error() {} };
  const { server } = createActionTagServer({
    env: environment,
    logger,
    fetchImpl: async (_url, options) => {
      deliveries.push(JSON.parse(options.body));
      return { ok: true, status: 200, json: async () => ({ MessageID: 'postmark-message-2' }) };
    },
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  t.after(() => new Promise((resolve) => server.close(resolve)));
  const port = server.address().port;

  const health = await request(port);
  assert.equal(health.status, 200);
  assert.deepEqual(health.body, { status: 'healthy', release: '0123456789ab', mail: 'configured' });

  const body = new URLSearchParams({
    name: 'Taylor',
    email: 'taylor@example.com',
    phone_cc: '+44',
    phone: '7000000000',
    message: 'This is a test enquiry.',
  }).toString();
  const response = await request(port, {
    method: 'POST',
    path: '/api/contact',
    body,
    headers: {
      Origin: 'https://actiontag.co.uk',
      'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8',
      'Content-Length': Buffer.byteLength(body),
    },
  });
  assert.equal(response.status, 200);
  assert.equal(response.body.ok, true);
  assert.equal(deliveries.length, 1);
  assert.equal(deliveries[0].To, 'sales@actiontag.co.uk');
  assert.equal(deliveries[0].Cc, 'office@actiontag.co.uk');
});

test('native service rejects untrusted origins and invalid fields', async (t) => {
  const { server } = createActionTagServer({ env: environment, logger: { info() {}, error() {} } });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  t.after(() => new Promise((resolve) => server.close(resolve)));
  const port = server.address().port;
  const body = new URLSearchParams({ name: '', email: 'invalid', message: '' }).toString();

  const forbidden = await request(port, {
    method: 'POST', path: '/api/contact', body,
    headers: { Origin: 'https://evil.example', 'Content-Type': 'application/x-www-form-urlencoded' },
  });
  assert.equal(forbidden.status, 403);

  const invalid = await request(port, {
    method: 'POST', path: '/api/contact', body,
    headers: { Origin: 'https://actiontag.co.uk', 'Content-Type': 'application/x-www-form-urlencoded' },
  });
  assert.equal(invalid.status, 422);
});
