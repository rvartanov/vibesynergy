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
    r.go("dashboard")
    r.say(
        "De startpagina laat zien hoe je ervoor staat en wat je vandaag doet.",
        "The start page shows how you are doing and what to do today.",
    )
    r.point(card(r, "Advies voor vandaag"))
    r.say(
        "Bovenaan: het advies voor vandaag, op basis van je plan en je herstel.",
        "At the top: today's advice, based on your plan and recovery.",
    )
    r.point(card(r, "Herstel"))
    r.say(
        "Herstel vergelijkt HRV, slaap en rusthartslag met wat normaal is voor jou.",
        "Recovery compares HRV, sleep and resting heart rate with your normal.",
    )
    ci = card(r, "Check-in")
    r.point(ci)
    r.say(
        "Vul elke ochtend de check-in in: 1 tot 5 per vraag.",
        "Fill in the check-in every morning: 1 to 5 per question.",
    )
    for i, v in enumerate(["4", "2", "4", "2", "4"]):
        r.click(ci.locator("button", has_text=v).nth(i), 0.3)
    r.say("Je antwoorden worden meteen bewaard.", "Your answers are saved right away.")
    r.click(ci.locator("button", has_text="+ Notitie"))
    r.type(ci.locator("textarea"), "Goed geslapen, benen voelen fris.")
    r.click(ci.locator("button", has_text="Notitie opslaan"))
    r.say(
        "Een notitie erbij helpt je coach het advies te verfijnen.",
        "A note helps your coach refine the advice.",
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
        "De + rechtsboven werkt op elke pagina: check-in, notitie of ongemak.",
        "The + at the top right works on every page: check-in, note or discomfort.",
    )
    dlg = r.pg.locator("dialog.quick-dialog")
    r.click(dlg.locator("button", has_text="+ Ongemak"))
    r.wait(0.8)
    r.say(
        "Kies + Ongemak om pijn of een klacht vast te leggen.",
        "Choose + Discomfort to log pain or a complaint.",
    )
    sels = dlg.locator("select:visible")
    r.select(sels.nth(0), "kuit")
    r.select(sels.nth(1), "links")
    r.select(sels.nth(2), "3")
    r.select(sels.nth(3), "tijdens")
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
    r.go("coach")
    r.say(
        "Training → Coach: advies, kennis, regels, geheugen en taken van je AI-coach.",
        "Training → Coach: your AI coach's advice, knowledge, rules, memory and tasks.",
    )
    r.click(r.pg.get_by_role("link", name="Instellingen", exact=True).last)
    r.say(
        "Instellingen: zet de coach aan en kies een AI-aanbieder.",
        "Settings: turn the coach on and choose an AI provider.",
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
    r.click(r.pg.locator("summary", has_text="Ochtendadvies").first)
    r.say(
        "Per taak stel je in wanneer de coach werkt, bijvoorbeeld het ochtendadvies om 9 uur.",
        "Per task you set when the coach runs, for example the morning advice at 9.",
        2.8,
    )
    r.click(r.pg.get_by_role("link", name="Geheugen"))
    r.say(
        "Geheugen: wat de coach over je onthoudt, en hoe hij praat.",
        "Memory: what the coach remembers about you, and how it talks.",
    )
    ta = r.pg.locator("textarea[name=instructions]")
    ta.fill("")
    r.type(ta, "Houd het kort en concreet.")
    r.pg.keyboard.press("Tab")
    r.say(
        "Notities voor je coach worden vanzelf bewaard.",
        "Notes for your coach are saved automatically.",
    )
    r.click(r.pg.get_by_role("button", name="Notitie toevoegen"))
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
        2.8,
    )
    r.click(r.pg.get_by_role("link", name="Kennis"))
    r.say(
        "Kennis: artikelen waarop de coach zich baseert. Wijzigingen keur jij goed.",
        "Knowledge: the articles the coach relies on. You approve changes.",
        2.6,
    )
    r.click(r.pg.get_by_role("link", name="Logboek"))
    r.say(
        "Logboek: elke wijziging in wat je coach weet, terug te kijken.",
        "Log: every change in what your coach knows, to look back on.",
    )


@scenario("health", "Herstel, slaap en lichaam", "Recovery, sleep and body")
def health(r: Rec):
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
        "Een nieuwe klacht leg je vast met + of op de startpagina (+ Pijn).",
        "Log a new complaint with + or on the start page (+ Pain).",
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


@scenario("sources", "Bronnen koppelen en synchroniseren", "Connecting and syncing sources")
def sources(r: Rec):
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
    r.click(r.pg.get_by_role("button", name="Herstel omhoog"))
    r.click(r.pg.get_by_role("button", name="Herstel omhoog"))
    r.say("Zo staat Herstel bovenaan.", "Now Recovery is near the top.", 1.6)
    r.go("settings/system")
    r.say(
        "Instellingen → Systeem: tijdzone, eenheden, weer, taal en thema.",
        "Settings → System: time zone, units, weather, language and theme.",
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
