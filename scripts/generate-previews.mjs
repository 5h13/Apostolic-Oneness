// Generates one static preview page per approved article at /a/<slug>.html.
// These exist purely so link-preview crawlers (iMessage, WhatsApp, Facebook,
// Twitter/X, etc.) — which never run JavaScript and never see the #hash
// part of a URL — have a real per-article title/description/OG tags to
// read. A real visitor who opens the page gets redirected straight into
// the normal single-page app at the matching #article-<slug> hash.
//
// Run via GitHub Actions (see .github/workflows/generate-previews.yml).
// Requires env vars SUPABASE_URL and SUPABASE_ANON_KEY.

import { mkdir, rm, writeFile } from 'node:fs/promises';

const SUPABASE_URL = process.env.SUPABASE_URL;
const SUPABASE_ANON_KEY = process.env.SUPABASE_ANON_KEY;
const SITE_ORIGIN = 'https://5h13.github.io';
const BASE_PATH = '/Apostolic-Oneness/';

if (!SUPABASE_URL || !SUPABASE_ANON_KEY) {
  console.error('Missing SUPABASE_URL or SUPABASE_ANON_KEY environment variables.');
  process.exit(1);
}

// --- Mirrors the slug logic in index.html exactly, so preview URLs match
// the app's own #article-<slug> links. Keep these two in sync if you ever
// change slugify()/articleSlug() in index.html. ---
function slugify(str) {
  return (str || '')
    .toString()
    .normalize('NFKD').replace(/[\u0300-\u036f]/g, '')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 60);
}
function shortCode(id) {
  return (id || '').replace(/-/g, '').slice(0, 8);
}
function articleSlug(a) {
  const parts = [slugify(a.author_name || a.author_email), slugify(a.title)].filter(Boolean);
  return parts.join('-') + (parts.length ? '-' : '') + shortCode(a.id);
}

function escapeHtml(str) {
  return (str || '').replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[c]));
}

function excerpt(body, maxLen = 160) {
  const plain = (body || '').replace(/\s+/g, ' ').trim();
  if (plain.length <= maxLen) return plain;
  return plain.slice(0, maxLen - 1).replace(/\s+\S*$/, '') + '…';
}

function fmtDate(iso) {
  try {
    return new Date(iso).toLocaleDateString('en-US', { year: 'numeric', month: 'short', day: 'numeric' });
  } catch (e) {
    return '';
  }
}

function pageHtml(a) {
  const slug = articleSlug(a);
  const title = escapeHtml(a.title || 'Untitled');
  const desc = escapeHtml(excerpt(a.body));
  const author = escapeHtml(a.author_name || a.author_email || '');
  const canonicalUrl = `${SITE_ORIGIN}${BASE_PATH}a/${slug}.html`;
  const redirectUrl = `${SITE_ORIGIN}${BASE_PATH}#article-${slug}`;

  return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>${title} — Apostolic Oneness</title>
<meta name="description" content="${desc}">

<meta property="og:type" content="article">
<meta property="og:site_name" content="Apostolic Oneness — The Flint">
<meta property="og:title" content="${title}">
<meta property="og:description" content="${desc}">
<meta property="og:url" content="${canonicalUrl}">
<meta property="og:image" content="${SITE_ORIGIN}${BASE_PATH}assets/logo-og.jpg">

<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="${title}">
<meta name="twitter:description" content="${desc}">
<meta name="twitter:image" content="${SITE_ORIGIN}${BASE_PATH}assets/logo-og.jpg">

<link rel="canonical" href="${canonicalUrl}">
<script>location.replace(${JSON.stringify(redirectUrl)});</script>
<style>
  body{background:#111;color:#ddd;font-family:Georgia,serif;max-width:640px;margin:60px auto;padding:0 20px;line-height:1.6;}
  a{color:#c9a44c;}
</style>
</head>
<body>
  <h1>${title}</h1>
  <p><em>By ${author}${a.created_at ? ' · ' + fmtDate(a.created_at) : ''}</em></p>
  <p>${desc}</p>
  <p><a href="${redirectUrl}">Continue reading →</a></p>
</body>
</html>
`;
}

async function main() {
  const url = `${SUPABASE_URL}/rest/v1/articles?select=id,title,body,author_name,author_email,created_at&status=eq.approved`;
  const res = await fetch(url, {
    headers: {
      apikey: SUPABASE_ANON_KEY,
      Authorization: `Bearer ${SUPABASE_ANON_KEY}`
    }
  });

  if (!res.ok) {
    console.error('Failed to fetch articles:', res.status, await res.text());
    process.exit(1);
  }

  const articles = await res.json();
  console.log(`Fetched ${articles.length} approved articles.`);

  // Wipe and regenerate from scratch each run, so a rejected/edited
  // article's old preview page never lingers.
  await rm('a', { recursive: true, force: true });
  await mkdir('a', { recursive: true });

  for (const a of articles) {
    const slug = articleSlug(a);
    await writeFile(`a/${slug}.html`, pageHtml(a), 'utf8');
  }

  console.log(`Wrote ${articles.length} preview pages to /a/`);
}

main().catch(err => {
  console.error(err);
  process.exit(1);
});
