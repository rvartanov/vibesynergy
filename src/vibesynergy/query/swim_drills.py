"""Swim technique drills, chosen systematically ("I don't know what to do
exactly"; it must be systematic and use my equipment, not all of it in one session).

The technique swim rotates through five focuses, one per ISO week: balance and rotation,
catch and pull, open water (sighting, Tarzan, deep-water starts, turning without a wall: the
triathlon focus), breathing, stroke length. Each focus has four drills for the 8x50 (two of
each) and one way to swim the 100s, with the equipment it needs. A drill whose equipment the
athlete lacks falls back to its plain version. The step's cue is short, so it fits the watch.
Background and sources: knowledge-base article ``swim-technique-drills``.
"""

from __future__ import annotations

from typing import Any

EQUIPMENT = ("pull_buoy", "kickboard", "fins", "snorkel", "paddles")
DEFAULT_EQUIPMENT = EQUIPMENT  # the athlete's bag (9-11): buoy, board, zoomers, snorkel, paddles
# general improvement and triathlon / open water; so open water has its week.
FOCI = ("balance", "catch", "open_water", "breathing", "length")
FOCUS_NAMES = {
    "balance": {"nl": "balans en rotatie", "en": "balance and rotation"},
    "catch": {"nl": "catch en trek", "en": "catch and pull"},
    "open_water": {"nl": "buitenzwemmen", "en": "open water"},
    "breathing": {"nl": "ademhaling", "en": "breathing"},
    "length": {"nl": "slaglengte", "en": "stroke length"},
}

# (cue nl, cue en, equipment, plain cue nl, plain cue en) per drill; cues fit the watch.
DRILLS: dict[str, list[tuple[str, str, tuple[str, ...], str, str]]] = {
    "balance": [
        # Front balance first, then the side, then switching sides, then the stroke (9-11).
        (
            "Superman · snorkel",
            "Superman · snorkel",
            ("snorkel",),
            "Superman glijden",
            "Superman glide",
        ),
        ("Zijkick · zoomers", "Side kick · fins", ("fins",), "Zijkick", "Side kick"),
        ("6-1-6 · zoomers", "6-1-6 · fins", ("fins",), "6-1-6", "6-1-6"),
        (
            "Rotatie · boei",
            "Rotation · buoy",
            ("pull_buoy",),
            "Rotatie, 3 slagen",
            "Rotation, 3 strokes",
        ),
    ],
    "catch": [
        ("Vuist / open hand", "Fist / open hand", (), "Vuist / open hand", "Fist / open hand"),
        ("Sculling · boei", "Sculling · buoy", ("pull_buoy",), "Sculling", "Sculling"),
        ("Catch-up · snorkel", "Catch-up · snorkel", ("snorkel",), "Catch-up", "Catch-up"),
        ("Eenarmig · zoomers", "Single arm · fins", ("fins",), "Eenarmig", "Single arm"),
    ],
    "open_water": [
        ("Kijken elke 6", "Sight every 6", (), "Kijken elke 6", "Sight every 6"),
        (
            "Tarzan 25 / crawl 25",
            "Tarzan 25 / crawl 25",
            (),
            "Tarzan 25 / crawl 25",
            "Tarzan 25 / crawl 25",
        ),
        ("Start diep water", "Deep-water start", (), "Start diep water", "Deep-water start"),
        ("Keren zonder muur", "Turn without wall", (), "Keren zonder muur", "Turn without wall"),
    ],
    "breathing": [
        ("Ademen 3-5-7", "Breathe 3-5-7", (), "Ademen 3-5-7", "Breathe 3-5-7"),
        ("Bilateraal 3", "Bilateral 3", (), "Bilateraal 3", "Bilateral 3"),
        (
            "Uitademen · snorkel",
            "Exhale · snorkel",
            ("snorkel",),
            "Bubbels uitademen",
            "Bubble exhale",
        ),
        (
            "Kick plank · ademen",
            "Board kick · breathe",
            ("kickboard",),
            "Benen, ademen opzij",
            "Kick, breathe side",
        ),
    ],
    "length": [
        ("Slagen tellen", "Count strokes", (), "Slagen tellen", "Count strokes"),
        ("Catch-up · zoomers", "Catch-up · fins", ("fins",), "Catch-up", "Catch-up"),
        ("Glijfase lang", "Long glide", (), "Glijfase lang", "Long glide"),
        ("Pull · boei+paddles", "Pull · buoy+paddles", ("pull_buoy", "paddles"), "Pull", "Pull"),
    ],
}
# How to swim the 100s in each focus week: apply the drill, with at most one piece of kit.
HUNDREDS = {
    "balance": ("Rustig · rotatie", "Easy · rotation", ()),
    "catch": ("Rustig · vroege grip", "Easy · early catch", ()),
    "open_water": ("Kijken elke 9", "Sight every 9", ()),
    "breathing": ("Rustig · ademen 3", "Easy · breathe 3", ()),
    "length": ("Pull · boei", "Pull · buoy", ("pull_buoy",)),
}


# How to do each drill ("put the instructions with the drills"), keyed by the
# plain Dutch cue; one or two short sentences: position, action, what to feel.
HOW: dict[str, dict[str, str]] = {
    "Superman glijden": {
        "nl": "Op je buik, beide armen gestrekt vooruit, gezicht naar beneden. Rustig trappen; "
        "hoofd laag, borst iets omlaag, heupen hoog.",
        "en": "On your front, both arms forward, face down. Kick easy; head low, chest slightly "
        "down, hips high.",
    },
    "Zijkick": {
        "nl": "Op je zij, onderste arm vooruit, bovenste arm langs je lichaam. Trap rustig; "
        "draai alleen je hoofd mee om te ademen.",
        "en": "On your side, lower arm forward, top arm along your body. Kick easy; turn only "
        "your head to breathe.",
    },
    "6-1-6": {
        "nl": "6 trappen op je zij, één slag naar de andere zij, weer 6 trappen. Draai je hele "
        "lijf in één keer, vanuit de heup.",
        "en": "6 kicks on your side, one stroke to the other side, 6 kicks again. Rotate your "
        "whole body at once, from the hips.",
    },
    "Rotatie, 3 slagen": {
        "nl": "Gewoon zwemmen met de boei tussen je benen; voel bij elke slag je schouder en heup "
        "samen naar boven draaien.",
        "en": "Swim normally with the buoy between your legs; feel shoulder and hip roll up "
        "together on each stroke.",
    },
    "Vuist / open hand": {
        "nl": "25 m met gesloten vuisten, 25 m met open hand. Zoek met de vuist steun met je "
        "onderarm; voel daarna hoeveel meer water je hand pakt.",
        "en": "25 m with closed fists, 25 m open hand. With fists, grip the water with your "
        "forearm; then feel how much more water your hand holds.",
    },
    "Sculling": {
        "nl": "Boei tussen je benen, armen vooruit, handen maken kleine achtjes net onder het "
        "water. Voel de druk op je handpalmen; langzaam vooruit is genoeg.",
        "en": "Buoy between your legs, arms forward, hands make small figure-eights just under "
        "the water. Feel the pressure on your palms; slow progress is fine.",
    },
    "Catch-up": {
        "nl": "Eén arm blijft voor gestrekt tot de andere hand hem aantikt. Zet je hand ver "
        "voor in en pak het water vroeg, met de elleboog hoog.",
        "en": "One arm stays forward until the other hand touches it. Enter far forward and "
        "catch early, elbow high.",
    },
    "Eenarmig": {
        "nl": "Eén arm zwemt, de andere ligt vooruit. Per 25 m wisselen. Adem naar de kant van "
        "de stilliggende arm; let op een lange, rustige trek.",
        "en": "One arm swims, the other stays forward; switch every 25 m. Breathe to the side "
        "of the still arm; make each pull long and calm.",
    },
    "Kijken elke 6": {
        "nl": "Elke 6 slagen kort vooruit kijken: ogen net boven water (krokodillenogen), dan "
        "opzij ademen. Hoofd zo laag mogelijk houden.",
        "en": "Every 6 strokes look forward briefly: eyes just above water (crocodile eyes), "
        "then breathe to the side. Keep the head as low as you can.",
    },
    "Tarzan 25 / crawl 25": {
        "nl": "25 m crawl met het hoofd boven water en kijkend vooruit (sterke beenslag), dan "
        "25 m gewone crawl. Goed voor oriënteren en drukte bij de start.",
        "en": "25 m front crawl with your head up, looking ahead (strong kick), then 25 m "
        "normal crawl. Good for sighting and a busy start.",
    },
    "Start diep water": {
        "nl": "Begin elke 50 vanuit stil liggen in het diepe, zonder afzet: 6-8 snelle slagen "
        "om op gang te komen, dan rustig door.",
        "en": "Start each 50 from still in deep water, no push-off: 6-8 quick strokes to get "
        "going, then settle.",
    },
    "Keren zonder muur": {
        "nl": "Keer halverwege en aan het eind zonder de muur te raken, zoals rond een boei: "
        "korte draai op je rug of zij en direct doorzwemmen.",
        "en": "Turn midway and at the end without touching the wall, as round a buoy: a short "
        "roll on your back or side and swim straight on.",
    },
    "Ademen 3-5-7": {
        "nl": "Per 25 m: adem om de 3, dan om de 5 slagen (of 7 als het lukt). Blaas rustig en "
        "volledig uit onder water.",
        "en": "Per 25 m: breathe every 3, then every 5 strokes (or 7 if you can). Exhale "
        "calmly and fully under water.",
    },
    "Bilateraal 3": {
        "nl": "Adem steeds om de 3 slagen, dus om en om links en rechts. Eén oog blijft in het "
        "water; niet optillen.",
        "en": "Breathe every 3 strokes, so left and right in turn. One eye stays in the water; "
        "do not lift.",
    },
    "Bubbels uitademen": {
        "nl": "Met snorkel: adem continu uit door je neus onder water en adem in door de "
        "snorkel. Ontspan je nek en schouders.",
        "en": "With the snorkel: exhale steadily through your nose under water and inhale "
        "through the snorkel. Relax neck and shoulders.",
    },
    "Benen, ademen opzij": {
        "nl": "Plank in één hand vooruit, op je zij trappen; draai je hoofd om opzij te ademen "
        "en terug naar het water.",
        "en": "Board in one hand forward, kick on your side; turn your head to breathe to the "
        "side and back into the water.",
    },
    "Slagen tellen": {
        "nl": "Tel je slagen per 25 m. Probeer bij de tweede 25 er één minder te maken, zonder "
        "langzamer te gaan.",
        "en": "Count your strokes per 25 m. On the second 25 try one fewer, without going slower.",
    },
    "Glijfase lang": {
        "nl": "Laat na elke slag je voorste arm even gestrekt liggen en glijd; begin de trek "
        "pas als de andere hand bijna vooraan is.",
        "en": "After each stroke let the front arm stay stretched and glide; start the pull "
        "only when the other hand is almost forward.",
    },
    "Pull": {
        "nl": "Boei tussen je benen (en paddles als je schouders goed voelen). Lange trek tot "
        "langs je heup, rustig tempo.",
        "en": "Buoy between your legs (and paddles if your shoulders feel fine). Long pull to "
        "your hip, easy pace.",
    },
}


def how(focus: str, lang: str = "nl") -> list[tuple[str, str]]:
    """(cue, instruction) for the four drills of a focus."""
    out = []
    for nl, en, kit, plain_nl, plain_en in DRILLS[focus]:
        have = all(k in DEFAULT_EQUIPMENT for k in kit)
        cue = (nl if have else plain_nl) if lang == "nl" else (en if have else plain_en)
        text = HOW.get(plain_nl, {}).get(lang, "")
        out.append((cue, text))
    return out


def focus_for_week(iso_week: int) -> str:
    return FOCI[(iso_week - 1) % len(FOCI)]


def drill_cues(
    focus: str, lang: str = "nl", equipment: tuple[str, ...] = DEFAULT_EQUIPMENT
) -> list[str]:
    """The four drill cues of a focus, each as plain when its kit is missing."""
    out = []
    for nl, en, kit, plain_nl, plain_en in DRILLS[focus]:
        have = all(k in equipment for k in kit)
        out.append((nl if have else plain_nl) if lang == "nl" else (en if have else plain_en))
    return out


def hundred_cue(
    focus: str, lang: str = "nl", equipment: tuple[str, ...] = DEFAULT_EQUIPMENT
) -> str:
    nl, en, kit = HUNDREDS[focus]
    if not all(k in equipment for k in kit):
        return "Rustig" if lang == "nl" else "Easy"
    return nl if lang == "nl" else en


def intro(focus: str, lang: str = "nl") -> str:
    name = FOCUS_NAMES[focus][lang]
    lines = [f"• {cue}: {text}" for cue, text in how(focus, lang) if text]
    if lang == "nl":
        head = (
            f"Techniek, focus deze week: {name}. Per drill 2x50; daarna de 100's rustig en "
            "pas toe wat je voelde. Licht = RPE 3-4, Middel = RPE 5-6."
        )
        return head + ("\nDrills:\n" + "\n".join(lines) if lines else "")
    head = (
        f"Technique, this week's focus: {name}. Each drill 2x50; then the 100s easy, applying "
        "what you felt. Easy = RPE 3-4, Moderate = RPE 5-6."
    )
    return head + ("\nDrills:\n" + "\n".join(lines) if lines else "")


def details(focus: str, lang: str = "nl") -> list[dict[str, Any]]:
    """For the coach and the page: each drill with its kit."""
    return [
        {"cue": nl if lang == "nl" else en, "equipment": list(kit)}
        for nl, en, kit, _pn, _pe in DRILLS[focus]
    ]
