"""
Pre-translates every approved article, plus all Academy content
(subjects, courses, lessons, quiz questions), into a set of target
languages using Argos Translate — an open-source engine that runs the
actual translation model locally in this CI job. There is no external
API call for translation itself, no key, no account, no subscription:
the model files are downloaded once (and cached between runs) from
Argos's public, free model index.

Article results are cached in Supabase's `translations` table.
Academy results are cached in `academy_translations`, keyed by
(content_type, content_id, language_code) — see the migration. Both
tables are public-read, write-only via the service-role key. The
browser never runs any translation itself; it only reads what this
script already produced.

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
from bs4 import BeautifulSoup

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


def hash_parts(*parts):
    """Generic version of source_hash for content with more than two
    translatable fields (Academy lessons, quiz questions, etc.)."""
    h = hashlib.sha256()
    for p in parts:
        h.update((p or "").encode("utf-8"))
        h.update(b"\x00")
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


# ===== Academy content: fetch/upsert helpers =====

def fetch_academy_subjects():
    url = f"{SUPABASE_URL}/rest/v1/academy_subjects?select=id,title"
    res = requests.get(url, headers=SB_HEADERS)
    res.raise_for_status()
    return res.json()


def fetch_academy_courses():
    url = f"{SUPABASE_URL}/rest/v1/academy_courses?select=id,title,description&status=eq.published"
    res = requests.get(url, headers=SB_HEADERS)
    res.raise_for_status()
    return res.json()


def fetch_academy_lessons():
    url = f"{SUPABASE_URL}/rest/v1/academy_lessons?select=id,title,type,html_content,essay_prompt"
    res = requests.get(url, headers=SB_HEADERS)
    res.raise_for_status()
    return res.json()


def fetch_academy_quiz_questions():
    url = f"{SUPABASE_URL}/rest/v1/academy_quiz_questions?select=id,prompt,options"
    res = requests.get(url, headers=SB_HEADERS)
    res.raise_for_status()
    return res.json()


def fetch_existing_academy_translations(content_type, content_ids):
    if not content_ids:
        return []
    ids_param = "(" + ",".join(content_ids) + ")"
    url = (
        f"{SUPABASE_URL}/rest/v1/academy_translations"
        f"?select=content_id,language_code,source_hash"
        f"&content_type=eq.{content_type}&content_id=in.{ids_param}"
    )
    res = requests.get(url, headers=SB_HEADERS)
    res.raise_for_status()
    return res.json()


def upsert_academy_translation(content_type, content_id, language_code, data, hash_):
    url = f"{SUPABASE_URL}/rest/v1/academy_translations?on_conflict=content_type,content_id,language_code"
    headers = {
        **SB_HEADERS,
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates",
    }
    payload = {
        "content_type": content_type,
        "content_id": content_id,
        "language_code": language_code,
        "data": data,
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


def translate_html(html_content, to_code):
    """Translates only the text nodes of an HTML fragment (lesson chapter
    content), leaving every tag and attribute untouched. Argos translates
    plain text only — running it on raw HTML risks mangling tags, so we
    walk the parsed tree and translate node-by-node instead."""
    soup = BeautifulSoup(html_content, "html.parser")
    for node in soup.find_all(string=True):
        text = str(node)
        if not text.strip():
            continue
        translated = translate(text, to_code)
        node.replace_with(translated)
    return str(soup)


def translate_academy_content(languages):
    print("\n--- Academy translations ---")
    translated_count = 0
    skipped_count = 0

    # Subjects
    subjects = fetch_academy_subjects()
    print(f"Fetched {len(subjects)} academy subjects.")
    existing = fetch_existing_academy_translations("subject", [s["id"] for s in subjects])
    existing_map = {(e["content_id"], e["language_code"]): e["source_hash"] for e in existing}
    for s in subjects:
        hash_ = hash_parts(s["title"])
        for code, label in languages:
            key = (s["id"], code)
            if existing_map.get(key) == hash_:
                skipped_count += 1
                continue
            try:
                data = {"title": translate(s["title"], code)}
                upsert_academy_translation("subject", s["id"], code, data, hash_)
                translated_count += 1
            except Exception as err:
                print(f"Failed translating subject {s['id']} to {code}: {err}", file=sys.stderr)

    # Courses
    courses = fetch_academy_courses()
    print(f"Fetched {len(courses)} published academy courses.")
    existing = fetch_existing_academy_translations("course", [c["id"] for c in courses])
    existing_map = {(e["content_id"], e["language_code"]): e["source_hash"] for e in existing}
    for c in courses:
        description = c.get("description") or ""
        hash_ = hash_parts(c["title"], description)
        for code, label in languages:
            key = (c["id"], code)
            if existing_map.get(key) == hash_:
                skipped_count += 1
                continue
            try:
                data = {"title": translate(c["title"], code)}
                if description.strip():
                    data["description"] = translate(description, code)
                upsert_academy_translation("course", c["id"], code, data, hash_)
                translated_count += 1
            except Exception as err:
                print(f"Failed translating course {c['id']} to {code}: {err}", file=sys.stderr)

    # Lessons
    lessons = fetch_academy_lessons()
    print(f"Fetched {len(lessons)} academy lessons.")
    existing = fetch_existing_academy_translations("lesson", [l["id"] for l in lessons])
    existing_map = {(e["content_id"], e["language_code"]): e["source_hash"] for e in existing}
    for l in lessons:
        html_content = l.get("html_content") or ""
        essay_prompt = l.get("essay_prompt") or ""
        hash_ = hash_parts(l["title"], html_content, essay_prompt)
        for code, label in languages:
            key = (l["id"], code)
            if existing_map.get(key) == hash_:
                skipped_count += 1
                continue
            try:
                data = {"title": translate(l["title"], code)}
                if html_content.strip():
                    data["content"] = translate_html(html_content, code)
                if essay_prompt.strip():
                    data["essay_prompt"] = translate(essay_prompt, code)
                upsert_academy_translation("lesson", l["id"], code, data, hash_)
                translated_count += 1
            except Exception as err:
                print(f"Failed translating lesson {l['id']} to {code}: {err}", file=sys.stderr)

    # Quiz questions
    questions = fetch_academy_quiz_questions()
    print(f"Fetched {len(questions)} academy quiz questions.")
    existing = fetch_existing_academy_translations("quiz_question", [q["id"] for q in questions])
    existing_map = {(e["content_id"], e["language_code"]): e["source_hash"] for e in existing}
    for q in questions:
        options = q.get("options") or []
        options_text = "\x1f".join(opt.get("text", "") for opt in options)
        hash_ = hash_parts(q["prompt"], options_text)
        for code, label in languages:
            key = (q["id"], code)
            if existing_map.get(key) == hash_:
                skipped_count += 1
                continue
            try:
                translated_options = [
                    {"key": opt.get("key"), "text": translate(opt.get("text", ""), code)}
                    for opt in options
                ]
                data = {
                    "prompt": translate(q["prompt"], code),
                    "options": translated_options,
                }
                upsert_academy_translation("quiz_question", q["id"], code, data, hash_)
                translated_count += 1
            except Exception as err:
                print(f"Failed translating quiz question {q['id']} to {code}: {err}", file=sys.stderr)

    print(f"Academy: wrote/updated {translated_count} translation rows, skipped {skipped_count} already up to date.")
    return translated_count, skipped_count


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

    print(f"Articles: wrote/updated {translated_count} translation rows, skipped {skipped_count} already up to date.")

    translate_academy_content(languages)


if __name__ == "__main__":
    main()
