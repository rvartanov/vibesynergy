# ruff: noqa: RUF001  (captions use the same typographic marks as the portal)
"""The use cases, each a short how-to video. Run: python scenarios.py [id ...]."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from rec import H, Rec, scenario

PW = (H / "test-password.txt").read_text().strip()


def card(r: Rec, title: str):
    return (
        r.pg.locator("section, article, .card").filter(has=r.pg.locator("h2", has_text=title)).last
    )


@scenario("login", "Inloggen en uitloggen", "Logging in and out", logged_in=False)
def login(r: Rec):
    r.go("dashboard")
    r.say(
        "Open VibeSynergy. Zonder sessie kom je op het inlogscherm.",
        "Open VibeSynergy. Without a session you land on the login screen.",
    )
    r.check("/login" in r.pg.url, "unauthenticated visit should land on /login")
    r.type(r.pg.locator("input[name=email]"), "tester@example.test")
    r.say("Vul je e-mailadres in…", "Enter your email address…", 1.2)
    r.type(r.pg.locator("input[name=password]"), PW)
    r.say("…en je wachtwoord.", "…and your password.", 1.2)
    r.click(r.pg.locator("button[type=submit]"))
    r.pg.wait_for_load_state("networkidle")
    r.wait(1.5)
    r.check("/login" not in r.pg.url, "login with the test password should leave /login")
    r.say(
        "Je bent binnen: de startpagina met het advies voor vandaag.",
        "You are in: the start page with today's advice.",
    )
    r.go("dashboard")
    r.click(r.pg.locator("header a.gear"))
    r.click(r.pg.locator("a", has_text="Account").first)
    r.say("Uitloggen doe je onder Instellingen → Account.", "Log out under Settings → Account.")
    r.point(r.pg.get_by_role("button", name="Uitloggen", exact=True))
    r.say(
        "Ook handig: inloggen met een link per mail, of 'Wachtwoord vergeten?' op het inlogscherm.",
        "Also handy: log in with a link by mail, or 'Forgot your password?' on the login screen.",
        3,
    )


@scenario("today", "Je dag: advies, herstel en check-in", "Your day: advice, recovery and check-in")
def today(r: Rec):
    # An AI coach's advice for today, so the card shows it (cleared again afterwards).
    # And an advisory AI coach's second opinion that differs from it (#118): one helper
    # seeds both.
    # And a badly recorded night, so the sleep check shows (#100).
    _demo("demo_coach_roles.py", "seed")
    _demo("demo_sleep_check.py", "seed")
    try:
        _today(r)
    finally:
        _demo("demo_sleep_check.py", "clear")
        _demo("demo_coach_roles.py", "clear")


def _today(r: Rec):
    r.go("dashboard")
    r.say(
        "De startpagina laat zien hoe je ervoor staat en wat je vandaag doet.",
        "The start page shows how you are doing and what to do today.",
    )
    r.point(card(r, "Advies voor vandaag"))
    r.say(
        "Bovenaan: het advies voor vandaag. Je AI-coach bepaalt de kleur, de veiligheid en de "
        "redenen; zonder geldig advies geven de vaste regels het, met de reden erbij.",
        "At the top: today's advice. Your AI coach decides the colour, the safety and the "
        "reasons; without valid advice the fixed rules give it, and say why.",
        3.2,
    )
    advice = card(r, "Advies voor vandaag")
    details = advice.locator("summary", has_text="Waarom dit advies").first
    r.check(details.count() > 0, "the advice should have its details")
    r.click(details)
    r.check(
        "Veranderd omdat" in (advice.locator("[data-advice-details]").first.text_content() or ""),
        "the details should say why the advice changed (#115)",
    )
    r.say(
        "Onder 'Waarom dit advies': de redenen, waarom het advies veranderde, waar het "
        "vandaan komt en waar je AI-coach en de regels verschillen.",
        "Under 'Why this advice': the reasons, why the advice changed, where it comes from "
        "and where your AI coach and the rules differ.",
        3.4,
    )
    r.click(details)
    verdict = advice.locator("[data-advice-state]").first
    r.check(verdict.count() > 0, "the advice should show its colour state (#147)")
    r.point(verdict)
    r.say(
        "De kleur met wat die betekent. Is je training van vandaag gedaan, dan staat hier "
        "'Gedaan voor vandaag', met je herstel in een kleine regel.",
        "The colour and what it means. Once today's training is done, it says 'Done for "
        "today', with your recovery in a small line.",
        3.2,
    )
    updated = advice.locator("[data-advice-updated]")
    r.check(updated.count() > 0, "the advice should say when it was updated")
    r.point(updated)
    r.check(
        "na je check-in" in (updated.first.text_content() or ""),
        "the update should say why it came, in plain words (#147)",
    )
    r.say(
        "Komen er nieuwe gegevens binnen, zoals je check-in of een training, dan maakt je "
        "AI-coach een nieuwe versie. Hier staat wanneer en waarom: 'Bijgewerkt, na je "
        "check-in'. Een melding krijg je alleen als het advies echt verandert.",
        "When new data comes in, like your check-in or a workout, your AI coach makes a new "
        "version. Here it says when and why: 'Updated, after your check-in'. You only get a "
        "notice when the advice really changes.",
        4.0,
    )
    second = advice.locator("[data-second-opinions]")
    r.check(second.count() > 0, "the advice card should list the second opinion")
    r.check(
        advice.locator("[data-second-opinion-differs]").count() > 0,
        "a second opinion that differs should be flagged",
    )
    if second.get_attribute("open") is None:
        r.click(second.locator("summary"))
    else:
        r.point(second)
    r.say(
        "Werkt er een tweede AI-coach mee, dan staat zijn second opinion erbij. Wijkt die af "
        "van je AI-coach, dan zie je dat meteen. Je eigen AI-coach heeft het laatste woord.",
        "When a second AI coach works with you, its second opinion is listed here. Where it "
        "differs from your AI coach, you see it at once. Your own AI coach has the last word.",
        3.6,
    )
    sleep = advice.locator("[data-sleep-check]")
    r.check(sleep.count() > 0, "the badly recorded night should show its sleep check (#100)")
    r.point(sleep)
    r.say(
        "VibeSynergy controleert elke nacht. Klopt een meting niet, dan zie je hier waarom, en "
        "tellen slaap, HRV of gereedheid van die nacht niet mee voor het advies.",
        "VibeSynergy checks every night. When a measurement looks wrong, you see why here, and "
        "that night's sleep, HRV or readiness don't count for the advice.",
        3.8,
    )
    r.click(sleep.get_by_role("button", name="Nee, het klopt"))
    r.wait(1.0)
    sleep = advice.locator("[data-sleep-check]")
    r.check(
        "goed gemeten" in (sleep.first.text_content() or ""),
        "after 'No, it's right' the page should say the night counts as measured",
    )
    r.point(sleep)
    r.say(
        "Jij beslist: 'Nee, het klopt' telt de nacht toch mee, 'Ja, onvolledig' laat hem weg. "
        "Ongedaan maken kan altijd.",
        "You decide: 'No, it's right' counts the night after all, 'Yes, incomplete' leaves it "
        "out. You can always undo it.",
        3.4,
    )
    r.click(sleep.get_by_role("button", name="Ongedaan maken"))
    r.wait(1.0)
    advice = card(r, "Advies voor vandaag")
    update = advice.get_by_role("button", name="Advies bijwerken")
    r.check(update.count() > 0, "the advice card should have Update advice")
    r.point(update)
    r.say(
        "Advies bijwerken haalt eerst al je bronnen op en vraagt dan je AI-coach om nieuw advies.",
        "Update advice first syncs all your sources, then asks your AI coach for new advice.",
        2.8,
    )
    # Tomorrow's advice from the evening run, under today's (#138).
    tomorrow = advice.locator("[data-advice-tomorrow]")
    r.check(tomorrow.count() > 0, "the start page should show tomorrow's advice (#138)")
    r.check(
        "Advies voor morgen" in (tomorrow.first.text_content() or ""),
        "tomorrow's card should be named after tomorrow",
    )
    r.point(tomorrow)
    r.say(
        "Heeft je AI-coach 's avonds het advies voor morgen al gemaakt, dan staat het hier, "
        "met de kleur en de dag waarvoor het is. De melding noemt dezelfde dag.",
        "Once your AI coach has made tomorrow's advice in the evening, it is here, with its "
        "colour and the day it is for. The notice names the same day.",
        3.4,
    )
    r.point(card(r, "Herstel"))
    r.say(
        "Herstel vergelijkt HRV, slaap en rusthartslag met wat normaal is voor jou.",
        "Recovery compares HRV, sleep and resting heart rate with your normal.",
    )
    ci = card(r, "Check-in")
    # A check-in from an earlier recording today: open it again, so the scales show.
    edit = ci.locator("button", has_text="Check-in wijzigen")
    if edit.count():
        edit.first.click()
        r.wait(0.5)
    r.point(ci)
    r.say(
        "Vul elke ochtend de check-in in: 1 tot 5 per vraag.",
        "Fill in the check-in every morning: 1 to 5 per question.",
    )
    for i, v in enumerate(["4", "2", "4", "2", "4"]):
        r.click(ci.locator(".ql-scale").nth(i).locator(f"button[data-value='{v}']"), 0.3)
    r.say("Je antwoorden worden meteen bewaard.", "Your answers are saved right away.")
    r.click(ci.locator("button", has_text="+ Notitie"))
    r.type(ci.locator("textarea"), "Goed geslapen, benen voelen fris.")
    r.click(ci.locator("button", has_text="Notitie opslaan"))
    r.say(
        "Een notitie erbij helpt je AI-coach het advies te verfijnen.",
        "A note helps your AI coach refine the advice.",
    )
    r.click(r.pg.get_by_role("link", name="Vorige dag"))
    r.say(
        "Met ‹ en › of de datum blader je naar andere dagen.",
        "Use ‹ and › or the date to move to other days.",
    )
    r.click(r.pg.get_by_role("link", name="Volgende dag"))
    r.scroll(900)
    r.say(
        "Verder naar beneden: de afgelopen nacht, lichaam, tijdlijn en beste prestaties.",
        "Further down: last night, body, timeline and best efforts.",
        2.6,
    )


@scenario("quicklog", "Snel vastleggen met +", "Quick logging with +")
def quicklog(r: Rec):
    r.go("dashboard")
    r.click(r.pg.locator("header button.quick-add"))
    r.say(
        "De + rechtsboven werkt op elke pagina: check-in, notitie of pijn.",
        "The + at the top right works on every page: check-in, note or pain.",
    )
    dlg = r.pg.locator("dialog.quick-dialog")
    r.click(dlg.locator("button", has_text="+ Pijn of ongemak"))
    r.wait(0.8)
    r.say(
        "Kies + Pijn of ongemak om pijn of een klacht vast te leggen.",
        "Choose + Pain or niggle to log pain or a complaint.",
    )
    form = dlg.locator("form[data-form=niggle]")
    r.select(form.locator("select[name=body_part]"), "kuit")
    r.select(form.locator("select[name=side]"), "links")
    r.select(form.locator("select[name=pain]"), "3")
    r.select(form.locator("select[name=timing]"), "tijdens")
    r.type(dlg.locator("input[name=note]:visible"), "Trekt bij het afzetten")
    r.say(
        "Kies waar het zit, welke kant, hoe erg (0–10) en wanneer je het voelt.",
        "Choose where it is, which side, how bad (0–10) and when you feel it.",
    )
    r.click(dlg.locator("button:visible", has_text="Ongemak vastleggen"))
    r.say(
        "Opgeslagen: de klacht staat nu bij Gezondheid → Klachten en blessures.",
        "Saved: the complaint now shows under Health → Complaints & injuries.",
    )
    r.pg.keyboard.press("Escape")
    r.go("injuries")
    r.check("Nu (0)" not in r.text(), "complaint from quick add should appear on injuries page")
    r.say(
        "Hier volg je hoe het gaat, tot de klacht over is.",
        "Here you follow how it goes until the complaint is gone.",
        2.6,
    )


@scenario("activities", "Activiteiten zoeken en bekijken", "Finding and reviewing activities")
def activities(r: Rec):
    r.go("activities")
    r.say(
        "Training → Activiteiten toont al je trainingen, de nieuwste eerst.",
        "Training → Activities lists all your workouts, newest first.",
    )
    sels = r.pg.locator("main select")
    r.select(sels.nth(0), "3 maanden")
    r.select(sels.nth(1), "Hardlopen (49)") if "Hardlopen (49)" in sels.nth(
        1
    ).inner_text() else r.select(sels.nth(1), sels.nth(1).locator("option").nth(1).inner_text())
    r.say("Kies een periode en een sport…", "Pick a period and a sport…", 1.5)
    r.type(r.pg.locator("main input[type=search]"), "tempo")
    r.say("…of zoek op naam.", "…or search by name.", 1.5)
    first = r.pg.locator("main a[href^='#/activity/']").first
    r.check(first.count() > 0, "search 'tempo' should list an activity")
    r.click(first)
    r.pg.wait_for_load_state("networkidle")
    r.wait(1.5)
    r.say(
        "Een activiteit: kaart, grafieken, ronden en zones.",
        "An activity: map, charts, laps and zones.",
    )
    feel = r.pg.locator("main select").nth(0)
    r.select(feel, feel.locator("option").nth(4).inner_text())
    r.say(
        "Geef aan hoe zwaar het voelde: dat telt mee in het advies.",
        "Say how hard it felt: it counts in the advice.",
    )
    r.scroll(700)
    r.say(
        "Scroll voor tempo, hartslag en de verdeling over je zones.",
        "Scroll for pace, heart rate and the split over your zones.",
    )
    r.scroll(900)
    r.wait(1.0)


@scenario("calendar", "Kalender: terugkijken en vooruitkijken", "Calendar: looking back and ahead")
def calendar(r: Rec):
    r.go("schedule")
    r.say(
        "Training → Kalender: per maand wat je deed en wat gepland staat.",
        "Training → Calendar: per month what you did and what is planned.",
    )
    day = r.pg.locator("main button[aria-label*='gedaan']").last
    r.click(day)
    r.say(
        "Klik op een dag om de trainingen van die dag te zien.",
        "Click a day to see that day's workouts.",
    )
    r.pg.keyboard.press("Escape")
    r.wait(0.6)
    r.click(r.pg.get_by_role("link", name="Week", exact=True))
    r.say("Wissel tussen maand en week.", "Switch between month and week.")
    r.click(r.pg.get_by_role("link", name="Vorige"))
    r.say(
        "Met Vorige en Volgende blader je; Vandaag brengt je terug.",
        "Previous and Next page through time; Today brings you back.",
    )
    r.click(r.pg.get_by_role("link", name="Vandaag"))


@scenario("planning", "Planning & voortgang", "Planning & progress")
def planning(r: Rec):
    r.go("planning")
    r.say(
        "Planning & voortgang: wat er deze week gepland is en wat je al deed.",
        "Planning & progress: what is planned this week and what you already did.",
    )
    r.click(r.pg.get_by_role("link", name="Vorige week"))
    r.say(
        "Blader door de weken om plan en uitvoering te vergelijken.",
        "Page through weeks to compare plan and execution.",
    )
    r.click(r.pg.get_by_role("link", name="Trainingsanalyse").first)
    r.say(
        "Trainingsanalyse: volume, intensiteit en belasting over een periode.",
        "Training analysis: volume, intensity and load over a period.",
    )
    r.scroll(800)
    r.wait(1.0)


@scenario("plan-builder", "Een trainingsplan bouwen", "Building a training plan")
def plan_builder(r: Rec):
    r.go("planning/builder")
    r.say(
        "Plan bouwen maakt een concept-schema naar je wedstrijd toe.",
        "Plan builder drafts a schedule towards your race.",
    )
    sels = r.pg.locator("main select")
    r.select(sels.nth(0), "Een nieuwe wedstrijd…")
    r.type(r.pg.locator("input[name=new_name]"), "Rotterdam halve marathon")
    r.pg.locator("input[name=new_date]").fill("2027-04-11")
    r.point(r.pg.locator("input[name=new_date]"))
    r.say(
        "Kies een wedstrijd of maak een nieuwe: naam en datum.",
        "Pick a race or add a new one: name and date.",
    )
    r.select(r.pg.locator("main select[name=sport]"), "Hardlopen")
    r.select(r.pg.locator("main select[name=distance]"), "Halve marathon")
    r.say("Kies de sport en de afstand.", "Choose the sport and the distance.", 1.5)
    hmin, hmax = r.pg.locator("input[name=hours_min]"), r.pg.locator("input[name=hours_max]")
    r.point(hmin)
    hmin.fill("4")
    r.point(hmax)
    hmax.fill("7")
    r.say(
        "Geef aan hoeveel uur per week je kunt trainen.", "Set how many hours a week you can train."
    )
    rest = r.pg.locator("input[name=rest_day]")
    r.click(rest.nth(0), 0.4)
    r.say("Vink je rustdag(en) aan.", "Tick your rest day(s).")
    r.click(r.pg.get_by_role("button", name="Maak een concept"))
    r.pg.wait_for_load_state("networkidle")
    r.wait(2)
    r.say(
        "Je krijgt een concept: bekijk het en neem het over als het klopt.",
        "You get a draft: review it and adopt it when it fits.",
        2.6,
    )
    r.scroll(700)


def _demo(script: str, action: str) -> None:
    """Seed or clear demo data with a helper next to this file (demo home only)."""
    import os
    import subprocess

    env = {
        **os.environ,
        "XDG_CONFIG_HOME": str(H / "config"),
        "XDG_DATA_HOME": str(H / "data"),
    }
    helper = Path(__file__).with_name(script)
    subprocess.run(["uv", "run", "python", str(helper), action], env=env, check=True)


def _demo_group_training(action: str) -> None:
    """Seed or clear the demo's RunSynergy training and "Training geven" session."""
    _demo("demo_group_training.py", action)


@scenario(
    "group-training",
    "Groepstraining geven met RunSynergy",
    "Coaching a group training with RunSynergy",
)
def group_training(r: Rec):
    _demo_group_training("seed")
    try:
        r.go("settings/sources")
        box = r.pg.locator("[data-source=runsynergy]")
        r.point(box.locator("h2"))
        r.say(
            "Geef je groepstrainingen? Koppel RunSynergy onder Instellingen → Bronnen.",
            "Do you coach group trainings? Connect RunSynergy under Settings → Sources.",
            2.6,
        )
        r.point(box.locator("input[name=runsynergy_url]"))
        r.say(
            "Plak hier de agendalink uit RunSynergy (Mijn gegevens → Agendalink).",
            "Paste the calendar link from RunSynergy here (My details → Calendar link).",
            2.8,
        )
        r.go("dashboard")
        block = r.pg.locator(".dash-group-trainings")
        block.wait_for(timeout=15000)
        r.check(block.count() == 1, "the group-training proposal shows on Today")
        r.point(block.locator("h3"))
        r.say(
            "Op Vandaag staat het schema van de groep als voorstel voor je sessie Training geven.",
            "Today shows the group schedule as a proposal for your Training geven session.",
            2.8,
        )
        r.click(block.locator("summary", has_text="Bekijk de workout"))
        r.say(
            "Inlopen en uitlopen tot je op Lap drukt, de kern op tijd om te fluiten, jij in Z1.",
            "Warm-up and cool-down until you press lap, the core on time to blow the whistle, "
            "you in Z1.",
            3.2,
        )
        r.click(block.get_by_role("button", name="Overnemen"))
        r.check("Overgenomen" in r.text(".dash-group-trainings"), "adopting says so")
        r.say(
            "Overnemen zet hem in je plan en op je horloge; hij telt als gepland.",
            "Adopt puts it in your plan and on your watch; it counts as planned.",
            2.8,
        )
    finally:
        _demo_group_training("clear")


@scenario("fitness", "Zones, drempels en vorm", "Zones, thresholds and form")
def fitness(r: Rec):
    r.go("zones")
    r.say(
        "Training → Fitheid: je zones en drempels per sport.",
        "Training → Fitness: your zones and thresholds per sport.",
    )
    r.click(r.pg.locator("summary", has_text="Zones, aanpassen").first)
    r.click(r.pg.get_by_role("button", name="Wijzig Drempelhartslag (LTHR)").first)
    r.say(
        "Wijzig een drempel na een test; de zones schuiven mee.",
        "Change a threshold after a test; the zones follow.",
    )
    form = r.pg.locator("main form:visible").filter(has=r.pg.locator("input[name=value]")).first
    r.type(form.locator("input[name=value]"), "168")
    r.select(form.locator("select").nth(1), "Test")
    for cb in form.locator("input[type=checkbox]:checked").all():
        cb.uncheck()
    r.click(form.get_by_role("button", name="Opslaan"))
    r.check("168" in r.text(), "saved LTHR 168 should show on the zones page")
    r.say(
        "Opgeslagen. De geschiedenis laat zien wanneer en waarom het veranderde.",
        "Saved. The history shows when and why it changed.",
    )
    r.click(r.pg.get_by_role("link", name="Vorm"))
    r.say(
        "Vorm: hoe fit en hoe fris je bent, en de lijn naar je wedstrijddag.",
        "Form: how fit and how fresh you are, and the line to race day.",
    )
    r.scroll(600)
    r.click(r.pg.get_by_role("link", name="Beste prestaties"))
    r.say(
        "Beste prestaties: je snelste tijden per afstand.",
        "Best efforts: your fastest times per distance.",
    )


@scenario("coach", "Je AI-coach instellen en gebruiken", "Setting up and using your AI coach")
def coach(r: Rec):
    # The day's advice with an advisory AI coach's second opinion beside it (#118).
    # Methods & rules and its check of the texts have their own manual (coach-methods).
    _demo("demo_coach_roles.py", "seed")
    # A real plan change, and an old copy to intervals.icu that stays hidden (#141).
    _demo("demo_plan_copy.py", "seed")
    try:
        _coach(r)
    finally:
        _demo("demo_plan_copy.py", "clear")
        _demo("demo_coach_roles.py", "clear")


def _coach(r: Rec):
    r.go("coach")
    r.say(
        "Training → Coach: advies, kennis, regels, geheugen en taken van je AI-coach.",
        "Training → Coach: your AI coach's advice, knowledge, rules, memory and tasks.",
    )
    second = r.pg.locator("[data-coach-advice] [data-second-opinions]")
    r.check(second.count() > 0, "the Coach page should list the second opinion")
    r.point(second)
    r.say(
        "Bij het advies van vandaag staat de second opinion van een adviserende AI-coach, "
        "met waar die afwijkt. In de berichten eronder heeft hij een eigen label.",
        "With today's advice you see the second opinion of an advisory AI coach, and where "
        "it differs. In the messages below it has its own label.",
        2.6,
    )
    chip = r.pg.locator("[data-second-opinion-chip]").first
    r.check(chip.count() > 0, "the second opinion should be marked in the messages")
    r.point(chip)
    r.wait(1.2)
    # Every advice is named after the day it is for (#138), with when and why (#147).
    label = r.pg.locator("[data-advice-label]", has_text="Advies voor morgen").first
    r.check(label.count() > 0, "the evening advice should be named after tomorrow (#138)")
    stamp = label.locator("xpath=..").locator("[data-advice-stamp]")
    r.check(
        "avondvooruitblik" in (stamp.first.text_content() or ""),
        "the evening advice should say it is the evening preview (#147)",
    )
    r.point(label.locator("xpath=.."))
    r.say(
        "Elk advies heet naar de dag waarvoor het is, met wanneer en waarom het kwam: de "
        "avondvooruitblik is het advies voor morgen.",
        "Every advice is named after the day it is for, with when and why it came: the "
        "evening preview is the advice for tomorrow.",
        2.6,
    )
    # A plan change says why and, under Details, what changed; a copy is no message (#141).
    change = r.pg.locator('article[data-entry-type="plan_change"]')
    r.check(change.count() == 1, "only the real plan change should be listed, not the copy")
    r.point(change)
    r.click(change.locator("details summary").first)
    # The whole message in view, reason and Details, not under the page header.
    change.first.evaluate("e => e.scrollIntoView({block: 'center'})")
    r.wait(0.3)
    r.check(
        change.locator("details", has_text="Sessie verplaatst").count() > 0,
        "the plan change's Details should say what changed",
    )
    r.say(
        "Een planaanpassing zegt waarom, en onder Details wat er veranderde. Een kopie naar "
        "intervals.icu is geen planaanpassing en staat hier niet.",
        "A plan change says why, and under Details what changed. A copy to intervals.icu is "
        "not a plan change and is not listed here.",
        2.6,
    )
    # The day thread (#115): the earlier version, marked replaced, and its own report.
    earlier = r.pg.locator("[data-coach-advice] [data-advice-versions]")
    r.check(earlier.count() > 0, "the Coach page should list the earlier version")
    r.click(earlier.locator("summary"))
    replaced = earlier.locator('[data-version-status="replaced"]').first
    r.check(replaced.count() > 0, "the earlier version should be marked replaced")
    r.point(replaced)
    r.say(
        "Eerdere versies van vandaag staan eronder, met tijd en waarom het advies veranderde. "
        "Een oudere versie is gemarkeerd als vervangen.",
        "Earlier versions of today are listed below, with the time and why the advice "
        "changed. An older version is marked replaced.",
        2.6,
    )
    r.click(earlier.locator("[data-advice-version] a").first)
    r.check(
        r.pg.locator("h1", has_text="Advies").count() > 0,
        "the report page of an advice should be called Advies (#147)",
    )
    banner = r.pg.locator('[data-thread-status="replaced"]')
    r.check(banner.count() > 0, "the report page should say the version was replaced")
    r.point(r.pg.locator(".advice-report-head").first)
    r.say(
        "Elke melding opent precies het advies waar hij over gaat, met de dag, wanneer en "
        "waarom. Is dat een oudere versie, dan staat dat erbij, met een link naar het "
        "huidige advies.",
        "Every notice opens exactly the advice it is about, with the day, when and why. When "
        "that is an older version, it says so, with a link to the current advice.",
        3.0,
    )
    r.go("coach")
    r.click(r.pg.get_by_role("link", name="Instellingen", exact=True).last)
    r.say(
        "Instellingen: zet je AI-coach aan en kies een AI-aanbieder.",
        "Settings: turn your AI coach on and choose an AI provider.",
    )
    r.point(
        r.pg.locator("details", has=r.pg.locator("summary", has_text="Anthropic")).first.locator(
            "input[type=password]"
        )
    )
    r.say(
        "Plak hier je eigen API-sleutel en kies 'Sleutel opslaan'.",
        "Paste your own API key here and choose 'Save key'.",
        2.6,
    )
    r.point(r.pg.locator("#coach-runner-advice-max"))
    r.say(
        "Nieuwe gegevens maken in de loop van de dag een nieuwe versie van het advies. Kies "
        "hoeveel updates per dag hoogstens; je ziet wat dat kost.",
        "New data makes a new version of the advice during the day. Choose how many updates "
        "a day at most; you see what that costs.",
        2.6,
    )
    contact_start = r.pg.locator("#coach-contact-start")
    r.check(contact_start.count() > 0, "Settings should show the contact window")
    r.point(contact_start)
    r.say(
        "Contactvenster: wanneer een AI-coach je mag bereiken, standaard van 7 tot 23 uur. "
        "Daarbuiten maakt hij zijn advies wel, maar meldingen wachten tot het venster opengaat.",
        "Contact window: when an AI coach may reach you, from 7 to 23 by default. Outside "
        "it the advice is still made, but notices wait until the window opens.",
        2.6,
    )
    r.click(r.pg.locator("summary", has_text="Ochtendadvies").first)
    r.say(
        "Per taak stel je in wanneer je AI-coach werkt, bijvoorbeeld het ochtendadvies om 7 uur.",
        "Per task you set when your AI coach runs, for example the morning advice at 7.",
        2.6,
    )
    r.click(r.pg.get_by_role("link", name="Geheugen"))
    r.say(
        "Geheugen: wat je AI-coach over je onthoudt, en hoe hij praat.",
        "Memory: what your AI coach remembers about you, and how it talks.",
    )
    ta = r.pg.locator("textarea[name=instructions]")
    ta.fill("")
    r.type(ta, "Houd het kort en concreet.")
    r.say(
        "Notities voor je AI-coach worden vanzelf bewaard.",
        "Notes for your AI coach are saved automatically.",
    )
    rows = r.pg.locator("textarea[name=constraint_text]")
    before = rows.count()
    # Straight from the notes to the button: the first click must work (#35).
    r.click(r.pg.get_by_role("button", name="Notitie toevoegen"))
    r.check(rows.count() == before + 1, "Notitie toevoegen: the first click added no note")
    r.select(
        r.pg.locator("main select")
        .filter(has=r.pg.locator("option", has_text="Beschikbaarheid"))
        .last,
        "Beschikbaarheid",
    )
    row = r.pg.locator("textarea[name=constraint_text]").last
    r.type(row, "Ik train het liefst 's ochtends voor het werk.")
    r.say(
        "Bij 'Wat te onthouden' leg je vaste afspraken vast, zoals wanneer je kunt trainen.",
        "Under 'What to remember' you add fixed facts, such as when you can train.",
        2.6,
    )
    r.click(r.pg.get_by_role("link", name="Kennis"))
    r.say(
        "Kennis: artikelen waarop je AI-coach zich baseert. Wijzigingen keur jij goed.",
        "Knowledge: the articles your AI coach relies on. You approve changes.",
        2.6,
    )
    r.click(r.pg.get_by_role("link", name="Logboek"))
    r.say(
        "Logboek: elke wijziging in wat je AI-coach weet, terug te kijken.",
        "Log: every change in what your AI coach knows, to look back on.",
    )


@scenario(
    "coach-methods",
    "Werkwijze & regels: de teksten van je AI-coach",
    "Methods & rules: the texts your AI coach follows",
)
def coach_methods(r: Rec):
    # A text VibeSynergy fixed without asking, and a content change that waits (#137).
    _demo("demo_kb_lint.py", "seed")
    try:
        _coach_methods(r)
    finally:
        _demo("demo_kb_lint.py", "clear")


def _coach_methods(r: Rec):
    r.go("coach/methods")
    r.say(
        "Coach → Werkwijze & regels: de teksten en regels die je AI-coach volgt.",
        "Coach → Methods & rules: the texts and rules your AI coach follows.",
    )
    waiting = r.pg.locator("[data-methods-waiting] .kb-proposal").first
    r.check(waiting.count() > 0, "a content change should wait on Methods & rules")
    r.check(waiting.locator(".coach-diff ins").count() > 0, "the waiting change shows a diff")
    r.point(waiting.locator(".coach-diff"))
    r.say(
        "Een wijziging die op jou wacht toont alleen de gewijzigde regels, met wat context, "
        "en de reden.",
        "A change waiting for you shows only the changed lines, with a little context, and "
        "the reason.",
        2.8,
    )
    r.click(waiting.locator("[data-full-text] summary"))
    r.say("De hele tekst staat één klik verder.", "The full text is one click away.", 1.8)
    fixed = r.pg.locator("[data-kb-editorial] li").first
    r.check(fixed.count() > 0, "the check of the texts should list the fix made without asking")
    r.point(fixed)
    r.say(
        "Verouderde namen, regelbereiken en vaste drempels verbetert VibeSynergy zelf. Hier "
        "zie je achteraf wat er veranderde.",
        "Outdated names, rule ranges and fixed thresholds VibeSynergy fixes itself. Here you "
        "see afterwards what changed.",
        3.0,
    )
    r.click(fixed.get_by_role("button", name="Ongedaan maken"))
    r.pg.wait_for_load_state("networkidle")
    undone = r.pg.locator("[data-kb-editorial] li").first
    r.check("Ongedaan gemaakt" in undone.inner_text(), "Undo should bring the old text back")
    r.point(undone)
    r.say(
        "Eén klik maakt het ongedaan; het logboek bewaart elke versie.",
        "One click undoes it; the change log keeps every version.",
        2.4,
    )


@scenario("health", "Herstel, slaap en lichaam", "Recovery, sleep and body")
def health(r: Rec):
    # A badly recorded night, so the sleep page lists it (#100).
    _demo("demo_sleep_check.py", "seed")
    try:
        _health(r)
    finally:
        _demo("demo_sleep_check.py", "clear")


def _health(r: Rec):
    r.go("recovery")
    r.say(
        "Gezondheid → Herstel: HRV, rusthartslag en je check-ins over tijd.",
        "Health → Recovery: HRV, resting heart rate and your check-ins over time.",
    )
    r.select(r.pg.locator("main select").first, "3 maanden")
    r.say("Kies een langere periode om trends te zien.", "Pick a longer period to see trends.")
    r.go("sleep")
    r.say(
        "Slaap: duur, score, fasen en bedtijden per nacht.",
        "Sleep: duration, score, stages and bedtimes per night.",
    )
    checks = card(r, "Slaapcheck")
    r.check(checks.count() > 0, "the sleep page should list the night the sleep check marked")
    r.point(checks)
    r.say(
        "Onder Slaapcheck: de nachten waarvan de meting mogelijk niet klopt, met de reden. "
        "Die tellen niet mee voor je gemiddelden.",
        "Under Sleep check: the nights whose measurement may be off, with the reason. They "
        "don't count for your averages.",
        3.4,
    )
    r.scroll(700)
    r.go("body")
    r.say(
        "Lichaam: gewicht en andere metingen, bijvoorbeeld van een Withings-weegschaal.",
        "Body: weight and other measurements, for example from a Withings scale.",
        2.6,
    )


@scenario("injuries", "Klachten en blessures bijhouden", "Tracking complaints and injuries")
def injuries(r: Rec):
    r.go("injuries")
    r.say(
        "Gezondheid → Klachten en blessures: wat nu pijn doet en hoe het verloopt.",
        "Health → Complaints & injuries: what hurts now and how it develops.",
    )
    sel = r.pg.locator("main select").first
    if sel.count():
        r.select(sel, "2")
        r.say(
            "Geef elke dag een nieuwe pijnscore: zo zie je of het beter gaat.",
            "Give a new pain score each day: you see whether it improves.",
        )
    log = r.pg.locator("summary", has_text="Pijnlog").first
    if log.count():
        r.click(log)
        r.say(
            "Het pijnlog toont alle scores op een rij.", "The pain log shows all scores in a row."
        )
    r.say(
        "Een nieuwe klacht leg je vast met + of op de startpagina (+ Pijn of ongemak).",
        "Log a new complaint with + or on the start page (+ Pain or niggle).",
        2.6,
    )


@scenario("insight", "Meetwaarden, correlaties en SQL", "Metrics, correlations and SQL")
def insight(r: Rec):
    r.go("metrics")
    r.say(
        "Inzicht → Meetwaarden: zet meetwaarden naast elkaar in één grafiek.",
        "Insight → Metrics: put metrics side by side in one chart.",
    )
    r.select(r.pg.locator("main select").first, "Stappen")
    r.say(
        "Voeg een meetwaarde toe en kies periode, gemiddelde en vergelijking.",
        "Add a metric and pick period, average and comparison.",
    )
    r.select(r.pg.locator("main select[name=smooth]"), "7-daags gemiddelde")
    r.go("correlations")
    r.say(
        "Correlaties: hangt het ene samen met het andere, ook een dag later?",
        "Correlations: is one thing related to another, even a day later?",
        2.6,
    )
    r.select(r.pg.locator("main select").nth(2), "de dag erna")
    r.go("sql")
    r.say(
        "SQL: stel je eigen vraag aan je gegevens (alleen lezen).",
        "SQL: ask your own question of your data (read-only).",
    )
    r.select(r.pg.locator("main select").first, "Hardloopafstand per maand")
    r.click(r.pg.get_by_role("button", name="Uitvoeren"))
    r.select(r.pg.locator("main select").nth(3), "Staven")
    r.say(
        "Kies een voorbeeld, voer het uit en toon het als grafiek.",
        "Pick an example, run it and show it as a chart.",
        2.6,
    )


@scenario("search-inbox", "Zoeken en 'Wacht op jou'", "Search and 'Waiting for you'")
def search_inbox(r: Rec):
    # A plan change an AI coach proposed and a second opinion that differs (#118), and texts
    # VibeSynergy fixed itself (#137).
    _demo("demo_coach_roles.py", "seed")
    _demo("demo_kb_lint.py", "seed")
    try:
        _search_inbox(r)
    finally:
        _demo("demo_kb_lint.py", "clear")
        _demo("demo_coach_roles.py", "clear")


def _search_inbox(r: Rec):
    r.go("dashboard")
    r.pg.keyboard.press("/")
    r.wait(1)
    r.say("Druk op / (of het vergrootglas) om te zoeken.", "Press / (or the magnifier) to search.")
    r.type(r.pg.locator("main input[type=search]"), "interval")
    r.pg.keyboard.press("Enter")
    r.pg.wait_for_load_state("networkidle")
    r.wait(1.2)
    r.check("interval" in r.text().lower(), "search for 'interval' should find activities")
    r.say(
        "Je zoekt in activiteiten, dagboek, coachberichten, kennis en materiaal.",
        "You search activities, diary, coach messages, knowledge and gear.",
        2.6,
    )
    r.click(r.pg.locator("header a[href='#/inbox']"))
    r.say(
        "Het belletje: 'Wacht op jou' toont voorstellen die alleen jij kunt goedkeuren.",
        "The bell: 'Waiting for you' lists proposals only you can approve.",
        2.8,
    )
    proposal = r.pg.locator("[data-plan-proposal]").first
    r.check(proposal.count() > 0, "a plan proposal should wait for a decision")
    r.point(proposal.get_by_role("button", name="Goedkeuren en doorvoeren"))
    r.say(
        "Een AI-coach wijzigt je plan niet zomaar: zijn wijziging wacht hier als voorstel. "
        "Keur je het goed, dan voert VibeSynergy het door; afwijzen kan ook.",
        "An AI coach does not just change your plan: its change waits here as a proposal. "
        "Approve it and VibeSynergy carries it out; you can also reject it.",
        3.6,
    )
    advice = r.pg.locator("main [data-advice-notice]").first
    r.check(advice.count() > 0, "the day's advice should be a notice (#147)")
    r.check(
        "bijgewerkt om" in (advice.text_content() or ""),
        "the advice notice should say when and why it was updated (#147)",
    )
    r.point(advice)
    r.say(
        "Bij de meldingen staat het advies van vandaag in één regel: wanneer en waarom het "
        "bijgewerkt is, en de eerste zin. Bovenaan het scherm staat dezelfde regel, met "
        "'Bekijk'.",
        "The notices hold today's advice in one line: when and why it was updated, and its "
        "first sentence. The top of the screen shows the same line, with 'View'.",
        3.4,
    )
    notice = r.pg.locator("a", has_text="Second opinion").first
    r.check(notice.count() > 0, "a second opinion that differs should be a notice")
    r.point(notice)
    r.say(
        "Bij de meldingen zie je ook wanneer een second opinion afwijkt van je AI-coach.",
        "The notices also say when a second opinion differs from your AI coach.",
        2.8,
    )
    texts = r.pg.locator("a", has_text="Teksten bijgewerkt").first
    r.check(texts.count() > 0, "the texts VibeSynergy fixed should be one notice")
    r.point(texts)
    r.say(
        "En welke teksten VibeSynergy zelf bijwerkte, zonder het te vragen.",
        "And which texts VibeSynergy updated itself, without asking.",
        2.4,
    )


@scenario("sources", "Bronnen koppelen en synchroniseren", "Connecting and syncing sources")
def sources(r: Rec):
    # A copy of the plan to intervals.icu, for the line on its card (#141).
    _demo("demo_plan_copy.py", "seed")
    try:
        _sources(r)
    finally:
        _demo("demo_plan_copy.py", "clear")


def _sources(r: Rec):
    r.go("settings/sources")
    r.say(
        "Instellingen → Bronnen: koppel Garmin, Strava, Withings, intervals.icu of een agenda.",
        "Settings → Sources: connect Garmin, Strava, Withings, intervals.icu or a calendar.",
        2.8,
    )
    r.point(r.pg.locator("input[name=garmin_email]"))
    r.say(
        "Garmin: log in met je Garmin-account en kies 'Garmin verbinden'.",
        "Garmin: sign in with your Garmin account and choose 'Connect Garmin'.",
    )
    r.point(r.pg.get_by_role("button", name="Nu bijwerken").first)
    r.say(
        "'Nu bijwerken' haalt meteen de nieuwste gegevens op.",
        "'Update now' fetches the latest data right away.",
    )
    r.point(r.pg.locator("input[name=garmin_mark]"))
    r.say(
        "Het teken voor de naam laat op je horloge zien welke workouts van VibeSynergy komen.",
        "The mark in front of the name shows on your watch which workouts come from VibeSynergy.",
        2.8,
    )
    r.click(r.pg.locator("input[name=garmin_mark_mode][value=per_type]"))
    r.say(
        "Of kies een teken per soort workout; de standaard staat al ingevuld.",
        "Or choose a mark per workout type; the defaults are filled in.",
        2.8,
    )
    r.point(r.pg.locator("input[name=garmin_marks][data-kind=multisport]"))
    r.say(
        "Multisport krijgt de tekens van zijn sporten, of één eigen teken.",
        "Multisport gets the marks of its sports, or one mark of its own.",
        2.6,
    )
    r.click(r.pg.locator("input[name=garmin_mark_mode][value=one]"))
    copy = r.pg.locator("[data-icu-plan-copy]")
    r.check(copy.count() > 0, "the intervals.icu card should show the last copy of the plan")
    r.point(copy)
    r.click(copy.locator("summary"))
    r.say(
        "intervals.icu krijgt een kopie van je plan. Hier zie je de laatste kopie en wat er "
        "gekopieerd is; in je berichten staan alleen echte planaanpassingen.",
        "intervals.icu gets a copy of your plan. Here you see the last copy and what was "
        "copied; your messages only list real plan changes.",
        3.4,
    )
    r.scroll(1200, 5)
    r.say(
        "Oude gegevens? Importeer de export-zip van Garmin of Strava.",
        "Older data? Import the export zip from Garmin or Strava.",
        2.6,
    )
    r.click(r.pg.get_by_role("link", name="Synchronisatie").first)
    r.say(
        "Synchronisatie: wat VibeSynergy terugschrijft, zoals een samenvatting in Strava.",
        "Sync: what VibeSynergy writes back, such as a summary in Strava.",
        2.8,
    )


@scenario("sharing", "Delen met je coach of een app", "Sharing with your coach or an app")
def sharing(r: Rec):
    r.go("settings/sharing")
    r.say(
        "Instellingen → Delen: geef iemand of een app toegang, zonder je wachtwoord.",
        "Settings → Sharing: give someone or an app access, without your password.",
        2.6,
    )
    title = r.pg.locator("input[name=title]")
    r.type(title, "Trainer Anna")
    form = title.locator("xpath=ancestor::form[1]")
    r.select(form.locator("select").first, "30 dagen")
    r.say(
        "Een deellink: kies een naam, hoe lang hij werkt en welke pagina's.",
        "A share link: choose a name, how long it works and which pages.",
    )
    r.click(form.get_by_role("button", name="Link maken"))
    r.pg.wait_for_load_state("networkidle")
    r.check("Trainer Anna" in r.text(), "new share link should be listed")
    r.say(
        "De link staat klaar om te kopiëren; intrekken kan altijd.",
        "The link is ready to copy; you can revoke it any time.",
    )
    roles = r.pg.locator("[data-coach-roles]")
    r.check(roles.count() > 0, "Coach apps should explain the roles")
    r.point(roles)
    r.say(
        "Elke app en elk token krijgt een rol: je eigen agent, de primaire AI-coach die je "
        "dagadvies geeft, of een adviserende AI-coach voor een second opinion en voorstellen.",
        "Each app and token gets a role: your own agent, the primary AI coach that gives your "
        "daily advice, or an advisory AI coach for a second opinion and proposals.",
        3.8,
    )
    r.point(roles.locator("select[data-role-client]").first)
    r.say(
        "Er is altijd precies één primaire AI-coach. Een nieuwe app begint als adviserend, "
        "tot jij kiest.",
        "There is always exactly one primary AI coach. A new app starts as advisory, until "
        "you choose.",
        2.8,
    )
    name = r.pg.locator("input[name=name]")
    r.type(name, "Mijn AI-assistent")
    r.say(
        "Een token geeft een AI-app (MCP) toegang. Kies wat het mag.",
        "A token gives an AI app (MCP) access. Choose what it may do.",
        2.6,
    )
    r.click(name.locator("xpath=ancestor::form[1]").locator("summary").first)
    r.wait(1.2)


@scenario("account", "Je account beveiligen", "Securing your account")
def account(r: Rec):
    r.go("settings/account")
    r.say(
        "Instellingen → Account: wachtwoord, tweestapsverificatie en sessies.",
        "Settings → Account: password, two-step verification and sessions.",
    )
    r.point(r.pg.get_by_role("button", name="Wachtwoord wijzigen"))
    r.say(
        "Wijzig hier je wachtwoord: minstens 12 tekens.",
        "Change your password here: at least 12 characters.",
    )
    cur = r.pg.get_by_label("Je wachtwoord, om de wijziging te bevestigen")
    r.type(cur, PW)
    r.say(
        "Voor tweestapsverificatie bevestig je eerst je huidige wachtwoord.",
        "For two-step verification, first confirm your current password.",
    )
    r.click(r.pg.get_by_role("button", name="Een authenticator-app gebruiken"))
    r.say(
        "Zet tweestapsverificatie aan met een authenticator-app of een code per mail.",
        "Turn on two-step verification with an authenticator app or a code by mail.",
        2.8,
    )
    r.point(r.pg.get_by_role("button", name="Overal uitloggen"))
    r.say(
        "Telefoon kwijt? 'Overal uitloggen' sluit alle sessies.",
        "Lost your phone? 'Log out everywhere' ends every session.",
    )
    r.click(r.pg.get_by_role("link", name="Meldingen").first)
    r.say(
        "Meldingen: kies per soort of je een melding in het portaal, per mail of push wilt.",
        "Alerts: choose per type whether you want a portal, mail or push alert.",
        2.8,
    )


@scenario("customize", "VibeSynergy naar je hand zetten", "Making VibeSynergy your own")
def customize(r: Rec):
    r.go("dashboard")
    r.scroll(4000, 4)
    r.click(r.pg.get_by_role("button", name="Startpagina indelen"))
    r.say(
        "Startpagina indelen: zet blokken aan of uit en verander de volgorde.",
        "Arrange start page: turn blocks on or off and change the order.",
    )
    up = r.pg.get_by_role("button", name="Herstel omhoog").first
    down = r.pg.get_by_role("button", name="Herstel omlaag").first
    # An earlier recording may have left Recovery at the top: move it back down first.
    for _ in range(2):
        if up.is_disabled() and down.is_enabled():
            down.click()
            r.wait(0.3)
    for _ in range(2):
        if up.is_enabled():
            r.click(up)
    r.say("Zo staat Herstel bovenaan.", "Now Recovery is near the top.", 1.6)
    r.go("settings/system")
    r.say(
        "Instellingen → Systeem: tijdzone, eenheden, dagadvies, weer, taal en thema.",
        "Settings → System: time zone, units, daily advice, weather, language and theme.",
    )
    daily = r.pg.locator("[data-daily-advice] select").first
    r.check(daily.count() > 0, "Settings → System should have Daily advice")
    r.point(r.pg.locator("[data-daily-advice]").first)
    r.say(
        "Dagadvies: van je AI-coach (aanbevolen), alleen de vaste regels, of uit.",
        "Daily advice: from your AI coach (recommended), the fixed rules only, or off.",
        2.6,
    )
    theme = r.pg.locator("main select").filter(has=r.pg.locator("option", has_text="Donker")).first
    r.select(theme, "Donker")
    r.say("Kies bijvoorbeeld het donkere thema.", "For example, choose the dark theme.")
    r.select(theme, "Systeem")
    r.click(r.pg.locator("summary", has_text="Menu aanpassen").first)
    r.say(
        "Menu aanpassen: verberg pagina's die je niet gebruikt of zet ze in een andere volgorde.",
        "Customize menu: hide pages you don't use or reorder them.",
        2.8,
    )
