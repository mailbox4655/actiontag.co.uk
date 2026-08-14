/**
 * Cloudflare Pages Function: POST /api/contact
 * Sends the contact form by email via Postmark.
 *
 * Required environment variables (set in the Cloudflare Pages dashboard):
 *   POSTMARK_SERVER_TOKEN  — Postmark server API token
 *   CONTACT_TO             — destination mailbox (e.g. admin@actiontag.co.uk)
 *   CONTACT_FROM           — a sender signature verified in Postmark
 */
export async function onRequestPost({ request, env }) {
  let form;
  try {
    form = await request.formData();
  } catch {
    return json({ error: 'bad request' }, 400);
  }

  // Honeypot: real users never fill this field
  if (form.get('company_website')) {
    return json({ ok: true }, 200);
  }

  const name = String(form.get('name') ?? '').trim().slice(0, 120);
  const email = String(form.get('email') ?? '').trim().slice(0, 200);
  const cc = String(form.get('phone_cc') ?? '').trim().slice(0, 8);
  const rawPhone = String(form.get('phone') ?? '').trim().slice(0, 40);
  const phone = rawPhone ? `${cc} ${rawPhone}`.trim() : '';
  const message = String(form.get('message') ?? '').trim().slice(0, 5000);

  if (!name || !message || !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) {
    return json({ error: 'validation' }, 422);
  }

  const res = await fetch('https://api.postmarkapp.com/email', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-Postmark-Server-Token': env.POSTMARK_SERVER_TOKEN,
    },
    body: JSON.stringify({
      From: env.CONTACT_FROM,
      To: env.CONTACT_TO,
      ReplyTo: email,
      Subject: `actiontag.co.uk enquiry from ${name}`,
      TextBody: [
        `Name:  ${name}`,
        `Email: ${email}`,
        phone ? `Phone: ${phone}` : null,
        '',
        message,
      ].filter(Boolean).join('\n'),
      MessageStream: 'outbound',
    }),
  });

  if (!res.ok) {
    console.error('Postmark error', res.status, await res.text());
    return json({ error: 'send failed' }, 502);
  }
  return json({ ok: true }, 200);
}

function json(body, status) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}
