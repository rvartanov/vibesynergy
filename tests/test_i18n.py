"""The portal in Dutch and English: every text the pages translate has a Dutch entry."""

from __future__ import annotations

import json
import re
from pathlib import Path

from vibesynergy.query import catalog, journal

STATIC = Path(__file__).parents[1] / "src" / "vibesynergy" / "portal" / "static"
# t("…") or N("…") (quoted, or `…` without ${…}): the English text is the key.
CALL = re.compile(
    r"""\b[tN]\(\s*(?:"((?:[^"\\]|\\.)*)"|'((?:[^'\\]|\\.)*)'|`((?:[^`\\$]|\\.)*)`)"""
)
PLACEHOLDER = re.compile(r"\{(\w+)\}")


def texts() -> dict[str, str]:
    """English text -> the file it first appears in."""
    found: dict[str, str] = {}
    for path in sorted((STATIC / "js").rglob("*.js")):
        for match in CALL.finditer(path.read_text()):
            raw = next(g for g in match.groups() if g is not None)
            text = json.loads(f'"{raw}"') if "\\" in raw else raw
            found.setdefault(text, path.name)
    return found


def dutch() -> dict[str, str]:
    return json.loads((STATIC / "i18n" / "nl.json").read_text())


def english() -> dict[str, str]:
    return json.loads((STATIC / "i18n" / "en.json").read_text())


def test_every_portal_text_has_a_dutch_translation() -> None:
    nl = dutch()
    missing = {text: where for text, where in texts().items() if text not in nl}
    assert not missing, f"add to static/i18n/nl.json: {json.dumps(missing, indent=1)}"


def test_nl_and_en_catalogues_have_the_same_keys() -> None:
    nl, en = dutch(), english()
    assert set(nl) == set(en), json.dumps(
        {"only_nl": sorted(set(nl) - set(en)), "only_en": sorted(set(en) - set(nl))},
        indent=1,
        ensure_ascii=False,
    )


# The design system's catalog keys: msg.* message types and sport.* sport names. They
# are ids, not English text, so the same word can mean two things ("Run": a sport, a button).
DESIGN_SYSTEM_KEY = re.compile(r"(msg|sport)\.[a-z]+")


def test_english_catalogue_keeps_the_source_text() -> None:
    for key, value in english().items():
        if DESIGN_SYSTEM_KEY.fullmatch(key):
            assert value.strip() and value != key, key
            continue
        assert value == key, key


def test_design_system_labels_are_in_both_catalogues() -> None:
    """Every message type and sport of the vendored icon set has an NL and EN label."""
    icons = (STATIC / "js" / "ds_icons.js").read_text()
    labels = set(re.findall(r'label: "((?:msg|sport)\.[a-z]+)"', icons))
    assert len(labels) == 16
    nl, en = dutch(), english()
    assert not labels - set(nl) and not labels - set(en), sorted(labels - (set(nl) & set(en)))
    assert nl["sport.run"] == "Hardlopen" and en["sport.run"] == "Run"


def test_every_portal_text_is_in_both_catalogues() -> None:
    keys = set(dutch()) & set(english())
    missing = {text: where for text, where in texts().items() if text not in keys}
    assert not missing, f"add to static/i18n/en.json and nl.json: {json.dumps(missing, indent=1)}"


def test_metric_labels_have_a_dutch_translation() -> None:
    nl = dutch()
    labels = {m.label for m in catalog.KNOWN.values() if m.label}
    missing = sorted(label for label in labels if label not in nl)
    assert not missing, f"add to static/i18n/nl.json: {missing}"


def test_translations_keep_the_placeholders() -> None:
    for english, translated in dutch().items():
        assert translated.strip(), english
        wanted = set(PLACEHOLDER.findall(english))
        assert set(PLACEHOLDER.findall(translated)) == wanted, english


def test_diary_choices_have_a_dutch_translation() -> None:
    nl = dutch()
    words = [*journal.SUGGESTED_TAGS, *journal.BODY_PARTS, *journal.SIDES, *journal.KINDS]
    words += [*journal.STATUSES, *journal.TIMINGS]
    missing = sorted({w.replace("_", " ") for w in words} - set(nl))
    assert not missing, f"add to static/i18n/nl.json: {missing}"


def test_server_texts_shown_in_the_portal_have_a_dutch_translation() -> None:
    """Fixed texts the server sends that the portal shows translated (t(value))."""
    from vibesynergy import alerts, history

    nl = dutch()
    texts = [journal.NOT_A_DIAGNOSIS, *(e["steps"] for e in history.EXPORTS.values())]
    texts += list(history.ARCHIVES.values())
    texts += [label for label, _ in alerts.KINDS.values()]
    missing = [text for text in texts if text not in nl]
    assert not missing, f"add to static/i18n/nl.json: {missing}"


SHADOW = re.compile(
    r"\((?:t|[\w\s,]*,\s*t)\)\s*(?:=>|\{)"
    r"|\b(?:let|const|var)\s+t\b|\bfor\s*\(\s*(?:const|let)\s+t\b"
)


def test_nothing_hides_the_translation_function() -> None:
    """A variable or parameter named t in a module that imports t() breaks the page."""
    for path in sorted((STATIC / "js").rglob("*.js")):
        text = path.read_text()
        if re.search(r"import \{[^}]*\bt\b[^}]*\} from", text):
            found = [m.group(0) for m in SHADOW.finditer(text)]
            assert not found, f"{path.name}: {found}"


def server_texts() -> dict[str, str]:
    """English text of every tr("…") call in the server code -> its file."""
    import ast

    found: dict[str, str] = {}
    for path in sorted((STATIC.parents[1]).rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "tr"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                found.setdefault(node.args[0].value, path.name)
    return found


def test_server_sentences_have_a_dutch_translation() -> None:
    nl = dutch()
    found = server_texts()
    assert len(found) > 30  # the Dashboard's and the injury rules' sentences
    missing = {text: where for text, where in found.items() if text not in nl}
    assert not missing, f"add to static/i18n/nl.json: {json.dumps(missing, indent=1)}"


def test_the_server_writes_dutch_when_asked() -> None:
    from vibesynergy.i18n import ENGLISH, translator

    nl = translator("nl")
    assert nl("No training for {n} days.", n=9) == "9 dagen niet getraind."
    assert ENGLISH("No training for {n} days.", n=9) == "No training for 9 days."
    assert translator("fr")("Well recovered") == "Well recovered"  # unknown: English


def test_oauth_ui_strings_have_a_dutch_translation() -> None:
    from vibesynergy.api import oauth

    nl = dutch()
    missing = [text for text in oauth.UI_STRINGS if text not in nl]
    assert not missing, f"add to static/i18n/nl.json: {missing}"


def test_oauth_ui_translations_keep_placeholders() -> None:
    from vibesynergy.api import oauth

    nl = dutch()
    for english in oauth.UI_STRINGS:
        translated = nl[english]
        assert set(PLACEHOLDER.findall(english)) == set(PLACEHOLDER.findall(translated)), english


def test_request_language_prefers_the_appearance_cookie() -> None:
    from types import SimpleNamespace

    from vibesynergy.i18n import request_language

    request = SimpleNamespace(
        cookies={"vibesynergy-language": "nl"},
        headers={"accept-language": "en-US,en;q=0.9"},
    )
    assert request_language(request) == "nl"
    auto = SimpleNamespace(
        cookies={"vibesynergy-language": "auto"}, headers={"accept-language": "nl"}
    )
    assert request_language(auto) == "nl"
    none = SimpleNamespace(cookies={}, headers={"accept-language": "de"})
    assert request_language(none) == "en"
    assert request_language(None) == "en"


def test_negotiate_language() -> None:
    from vibesynergy.i18n import negotiate_language

    assert negotiate_language(None) == "en"
    assert negotiate_language("") == "en"
    assert negotiate_language("nl") == "nl"
    assert negotiate_language("en-GB") == "en"
    assert negotiate_language("de-DE,de;q=0.9") == "en"
    assert negotiate_language("nl;q=0, en;q=0.8") == "en"
    assert negotiate_language("nl;q=0") == "en"
    assert negotiate_language("en;q=0, nl;q=1") == "nl"
    assert negotiate_language("nl;q=2") == "en"
    assert negotiate_language("nl;q=-0.1") == "en"
    assert negotiate_language("nl;q=bad") == "en"
    assert negotiate_language("fr, nl;q=0.9, en;q=0.8") == "nl"
    assert negotiate_language("fr, en;q=0.5, nl;q=0.9") == "nl"
    assert negotiate_language("nl;q=NaN") == "en"
    assert negotiate_language("nl;q=Infinity") == "en"
    assert negotiate_language("nl;q=-Infinity") == "en"
    assert negotiate_language("en;q=0, nl;q=NaN") == "en"


def test_portal_language_persists_like_theme_and_translates() -> None:
    """localStorage + catalogues: Dutch after persist, English after switching back."""
    import subprocess

    script = r"""
import fs from 'node:fs';
const store = {};
globalThis.localStorage = {
  getItem: (key) => (key in store ? store[key] : null),
  setItem: (key, value) => { store[key] = String(value); },
};
globalThis.document = { documentElement: { lang: 'en' }, cookie: '' };
globalThis.fetch = async (url) => {
  const href = String(url);
  const file = href.includes('nl.json') ? './i18n/nl.json' : './i18n/en.json';
  return { json: async () => JSON.parse(fs.readFileSync(file, 'utf8')) };
};
const {
  persistLanguage, storedLanguage, resolveLanguage, setLanguage, language, t,
} = await import('./js/i18n.js');
persistLanguage('nl');
if (storedLanguage() !== 'nl') throw new Error('stored');
if (resolveLanguage(storedLanguage(), 'en') !== 'nl') throw new Error('resolve stored');
if (resolveLanguage(null, 'nl') !== 'nl') throw new Error('resolve prefs');
if (resolveLanguage(null, 'auto', 'en') !== 'en') throw new Error('resolve auto');
await setLanguage(storedLanguage());
if (language() !== 'nl' || t('Appearance') !== 'Weergave' || t('Settings') !== 'Instellingen') {
  throw new Error('dutch');
}
persistLanguage('en');
await setLanguage(storedLanguage());
if (language() !== 'en' || t('Appearance') !== 'Appearance') throw new Error('english');
process.stdout.write('ok\n');
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=STATIC,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    assert result.stdout.strip() == "ok"
