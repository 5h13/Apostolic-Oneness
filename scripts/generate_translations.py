"""
Pre-translates every approved article into a set of target languages
using Argos Translate — an open-source engine that runs the actual
translation model locally in this CI job. There is no external API
call for translation itself, no key, no account, no subscription:
the model files are downloaded once (and cached between runs) from
Argos's public, free model index.

Results are cached in Supabase's `translations` table (public-read,
write-only via the service-role key — see the migration). The browser
never runs any translation itself; it only reads what this script
already produced.

Run via GitHub Actions (see .github/workflows/generate-translations.yml).
Requires env vars:
    SUPABASE_URL
    SUPABASE_SERVICE_ROLE_KEY   (NOT the anon key — bypasses RLS for
        writes; keep it a GitHub Actions secret only, never client-side)
"""

import datetime
import hashlib
import os
import sys

import requests
import argostranslate.package
import argostranslate.translate

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")

# Edit this list any time to add/remove target languages — no other
# code changes needed here beyond keeping index.html's LANGUAGES
# constant in sync for the dropdown. Whether each is actually usable
# depends on whether Argos publishes an en->code model — checked live
# below, so an unavailable language is skipped, not a hard failure.
DESIRED_LANGUAGES = [
    ("tl", "Filipino"),
    ("vi", "Vietnamese"),
    ("id", "Indonesian"),
    ("th", "Thai"),
    ("hi", "Hindi"),
    ("es", "Spanish"),
]

if not SUPABASE_URL or not SERVICE_ROLE_KEY:
    print("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY", file=sys.stderr)
    sys.exit(1)

SB_HEADERS = {
    "apikey": SERVICE_ROLE_KEY,
    "Authorization": f"Bearer {SERVICE_ROLE_KEY}",
}


def source_hash(title, body):
    h = hashlib.sha256()
    h.update(title.encode("utf-8"))
    h.update(b"\x00")
    h.update(body.encode("utf-8"))
    return h.hexdigest()


def fetch_approved_articles():
    url = f"{SUPABASE_URL}/rest/v1/articles?select=id,title,body&status=eq.approved"
    res = requests.get(url, headers=SB_HEADERS)
    res.raise_for_status()
    return res.json()


def fetch_existing_translations(article_ids):
    if not article_ids:
        return []
    ids_param = "(" + ",".join(article_ids) + ")"
    url = f"{SUPABASE_URL}/rest/v1/translations?select=article_id,language_code,source_hash&article_id=in.{ids_param}"
    res = requests.get(url, headers=SB_HEADERS)
    res.raise_for_status()
    return res.json()


def upsert_translation(article_id, language_code, title, body, hash_):
    url = f"{SUPABASE_URL}/rest/v1/translations?on_conflict=article_id,language_code"
    headers = {
        **SB_HEADERS,
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates",
    }
    payload = {
        "article_id": article_id,
        "language_code": language_code,
        "translated_title": title,
        "translated_body": body,
        "source_hash": hash_,
        "updated_at": datetime.datetime.utcnow().isoformat() + "Z",
    }
    res = requests.post(url, headers=headers, json=payload)
    res.raise_for_status()


def ensure_language_installed(to_code):
    """Downloads and installs the en->to_code Argos model if available
    and not already installed. Returns True if usable, False if Argos
    doesn't publish this language pair."""
    installed = argostranslate.package.get_installed_packages()
    if any(p.from_code == "en" and p.to_code == to_code for p in installed):
        return True

    available = argostranslate.package.get_available_packages()
    match = next((p for p in available if p.from_code == "en" and p.to_code == to_code), None)
    if match is None:
        return False

    print(f"Downloading Argos model: en -> {to_code} ...")
    download_path = match.download()
    argostranslate.package.install_from_path(download_path)
    return True


def translate(text, to_code):
    installed_languages = argostranslate.translate.get_installed_languages()
    from_lang = next(l for l in installed_languages if l.code == "en")
    to_lang = next(l for l in installed_languages if l.code == to_code)
    translation = from_lang.get_translation(to_lang)
    return translation.translate(text)


def main():
    print("Updating Argos package index...")
    argostranslate.package.update_package_index()

    languages = []
    unsupported = []
    for code, label in DESIRED_LANGUAGES:
        if ensure_language_installed(code):
            languages.append((code, label))
        else:
            unsupported.append(label)

    if unsupported:
        print(
            f"Skipping languages Argos doesn't currently publish a model for: {', '.join(unsupported)}. "
            f"These simply aren't available yet from the open model index — nothing broken, just not offered."
        )
    if not languages:
        print("None of the desired languages have an available Argos model. Nothing to do.")
        return

    print(f"Translating into: {', '.join(label for _, label in languages)}")

    articles = fetch_approved_articles()
    print(f"Fetched {len(articles)} approved articles.")

    existing = fetch_existing_translations([a["id"] for a in articles])
    existing_map = {(e["article_id"], e["language_code"]): e["source_hash"] for e in existing}

    translated_count = 0
    skipped_count = 0

    for article in articles:
        hash_ = source_hash(article["title"], article["body"])

        for code, label in languages:
            key = (article["id"], code)
            if existing_map.get(key) == hash_:
                skipped_count += 1
                continue
            try:
                translated_title = translate(article["title"], code)
                translated_body = translate(article["body"], code)
                upsert_translation(article["id"], code, translated_title, translated_body, hash_)
                translated_count += 1
            except Exception as err:
                print(f"Failed translating article {article['id']} to {code}: {err}", file=sys.stderr)

    print(f"Wrote/updated {translated_count} translation rows, skipped {skipped_count} already up to date.")


if __name__ == "__main__":
    main()
