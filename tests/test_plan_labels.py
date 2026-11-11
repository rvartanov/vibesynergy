"""Calendar plan labels in the portal."""

import json
import subprocess
from pathlib import Path


def test_mirror_plan_kind_uses_connected_calendar_name():
    static = Path(__file__).parents[1] / "src" / "vibesynergy" / "portal" / "static"
    script = """
import fs from 'node:fs';
import { setLanguage } from './js/i18n.js';
import { planKind } from './js/plan_labels.js';
globalThis.document = { documentElement: { lang: 'en' } };
globalThis.fetch = async (url) => ({ json: async () => {
  const file = String(url).includes('nl.json') ? './i18n/nl.json' : './i18n/en.json';
  return JSON.parse(fs.readFileSync(file, 'utf8'));
}});
const item = { kind: 'mirror', extra: { calendar_name: 'Workouts' } };
for (const language of ['en', 'nl']) {
  await setLanguage(language);
  process.stdout.write(JSON.stringify({ language, label: planKind(item) }) + '\\n');
}
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=static,
        capture_output=True,
        text=True,
        check=True,
    )
    labels = {
        json.loads(line)["language"]: json.loads(line)["label"]
        for line in result.stdout.splitlines()
    }
    assert labels["en"] == "Calendar: Workouts"
    assert labels["nl"] == "Agenda: Workouts"
