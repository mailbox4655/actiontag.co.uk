const EMAIL_PATTERN = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;

function required(env, name) {
  const value = String(env[name] ?? '').trim();
  if (!value) throw new Error(`Missing required environment variable ${name}`);
  return value;
}

function parseAddressList(value, name) {
  const addresses = value.split(',').map((item) => item.trim()).filter(Boolean);
  if (!addresses.length || addresses.some((address) => !EMAIL_PATTERN.test(address))) {
    throw new Error(`${name} must contain a comma-separated list of email addresses`);
  }
  return addresses;
}

export function loadContactConfiguration(env) {
  const release = required(env, 'ACTIONTAG_RELEASE');
  if (!/^[0-9a-f]{12}$/.test(release)) {
    throw new Error('ACTIONTAG_RELEASE must be a 12-character lowercase Git identity');
  }

  const trustedHosts = required(env, 'ACTIONTAG_TRUSTED_HOSTS')
    .split(',')
    .map((host) => host.trim().toLowerCase())
    .filter(Boolean);
  if (!trustedHosts.length || trustedHosts.some((host) => !/^[a-z0-9.-]+$/.test(host))) {
    throw new Error('ACTIONTAG_TRUSTED_HOSTS contains an invalid host name');
  }

  const fromEmail = required(env, 'POSTMARK_FROM_EMAIL');
  if (!EMAIL_PATTERN.test(fromEmail)) {
    throw new Error('POSTMARK_FROM_EMAIL is not a valid email address');
  }

  return Object.freeze({
    release,
    trustedHosts: new Set(trustedHosts),
    postmarkToken: required(env, 'POSTMARK_SERVER_TOKEN'),
    fromEmail,
    fromName: required(env, 'POSTMARK_FROM_NAME'),
    messageStream: required(env, 'POSTMARK_MESSAGE_STREAM'),
    to: parseAddressList(required(env, 'CONTACT_TO'), 'CONTACT_TO'),
    cc: parseAddressList(required(env, 'CONTACT_CC'), 'CONTACT_CC'),
  });
}

function field(params, name, maximum) {
  return String(params.get(name) ?? '').trim().slice(0, maximum);
}

function singleLine(value) {
  return value.replace(/[\u0000-\u001F\u007F]+/g, ' ').trim();
}

export function parseContactSubmission(params) {
  if (field(params, 'company_website', 256)) {
    return { ignored: true };
  }

  const name = singleLine(field(params, 'name', 120));
  const email = field(params, 'email', 200);
  const callingCode = singleLine(field(params, 'phone_cc', 8));
  const localPhone = singleLine(field(params, 'phone', 40));
  const message = field(params, 'message', 5000);

  if (!name || !message || !EMAIL_PATTERN.test(email)) {
    return { error: 'validation' };
  }

  return {
    name,
    email,
    phone: localPhone ? `${callingCode} ${localPhone}`.trim() : '',
    message,
  };
}

export async function deliverContactMessage({ submission, configuration, correlationId, fetchImpl }) {
  const payload = {
    From: `${configuration.fromName} <${configuration.fromEmail}>`,
    To: configuration.to.join(','),
    Cc: configuration.cc.join(','),
    ReplyTo: submission.email,
    Subject: `actiontag.co.uk enquiry from ${submission.name}`,
    TextBody: [
      `Name:  ${submission.name}`,
      `Email: ${submission.email}`,
      submission.phone ? `Phone: ${submission.phone}` : null,
      '',
      submission.message,
    ].filter((line) => line !== null).join('\n'),
    MessageStream: configuration.messageStream,
    Tag: 'actiontag-contact',
    Metadata: { correlation_id: correlationId, site: 'actiontag.co.uk' },
  };

  const response = await fetchImpl('https://api.postmarkapp.com/email', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-Postmark-Server-Token': configuration.postmarkToken,
    },
    body: JSON.stringify(payload),
    signal: AbortSignal.timeout(15_000),
  });

  if (!response.ok) {
    throw new Error(`Postmark rejected the contact message with HTTP ${response.status}`);
  }
  const result = await response.json();
  if (!result || typeof result.MessageID !== 'string' || !result.MessageID) {
    throw new Error('Postmark accepted the request without returning MessageID');
  }
  return { messageId: result.MessageID };
}
