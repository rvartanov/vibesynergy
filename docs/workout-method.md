<!-- Seed of the knowledge-base article `workout-method` (#142). The knowledge base is the live
source: migration 0058 copies this file in once, when the article is missing, and from then on
the article changes only through the knowledge base (a proposal the athlete approves). A later
edit of this file does not change an install's article. The leading comment is not copied. -->
# Trainingsmethode: waarden van de workoutbibliotheek

## Wat dit is
De workoutbibliotheek (`vibesynergy_workout_library`, de planbouwer) bouwt trainingen uit
sjablonen. De code kent alleen de **opbouw**: welke stappen, in welke volgorde, welke herhaling
en hoe dat naar intervals.icu, Zwift en Garmin gaat. **Alle inhoud** staat hier: cadansband,
aanwijzingen (cues), intensiteit (% FTP, % LTHR, hartslag- of tempozone), minuten van
warming-up en cooling-down, afstanden, rust, oefeningen, namen en beschrijvingen.

Een sjabloon leest deze waarden op het moment dat het een training maakt. Een nieuwe versie van
dit artikel geldt dus meteen voor elke nieuwe training, zonder code of update.

## Wijzigen
- Een AI-coach stelt een wijziging voor met `vibesynergy_kb_propose_change` (kind `article`,
  target `workout-method`, de hele nieuwe tekst in `body_md`). Het is een coachingsfeit: de
  atleet keurt het goed onder Wachten op jou, met de wijziging als diff.
- Na goedkeuring gelden de nieuwe waarden voor nieuwe trainingen. Geplande trainingen die uit
  een sjabloon kwamen, worden alleen op verzoek opnieuw opgebouwd
  (`vibesynergy_workout_rerender`, vanaf morgen). Gedane trainingen veranderen nooit.
- Een waarde die hier ontbreekt of niet klopt, komt uit de meegeleverde basiswaarden; de
  lint van de kennisbank noemt haar, en `vibesynergy_explain` (metric `workout:<sjabloon>`)
  laat per waarde zien waar ze vandaan kwam.

## Vorm
Het blok hieronder is TOML, één tabel per sjabloon (de id uit de catalogus):
- `name`, `purpose`, `cue`: tekst in `nl` en `en`; `{minutes}`, `{reps}` enz. vult de
  bibliotheek in. `cue` is de aanwijzing boven de stappen.
- `phases`, `load` (easy, moderate, hard), `easier` (het lichtere sjabloon), `kb_rules`,
  `params` (de standaardwaarden; welke parameters er zijn, bepaalt de opbouw).
- `steps`: per stap `minutes`, `seconds` of `metres`; een doel als `ftp_pct`, `lthr_pct`,
  `hr_zone` of `pace_zone` (`[laag, hoog]` of `[waarde]`); `cadence` (`[laag, hoog]`,
  `[]` is geen cadans); `cue` (`""` is geen cue). Een stap zonder duur krijgt die van de
  opbouw (de rest van de tijd, of een parameter). `target` noemt alleen het soort doel
  wanneer de grenzen parameters zijn; `min_minutes` is de kortste duur; `cue_repeat` zegt
  hoe vaak een techniekcue achter elkaar komt.
- `exercises` (kracht): per oefening `code` (Garmin), `sets` en `reps` (een getal, of
  `"sets"` / `"reps"` voor de parameter), `reps_max`, `time`, `side`, `rest_s` en `label`.

```toml
[run_recovery]
name = { nl = "Herstelloop {minutes}'", en = "Recovery run {minutes}'" }
purpose = { nl = "Herstel na een zware dag, heel rustig.", en = "Recovery after a hard day, very easy." }
cue = { nl = "Heel rustig, Z1; je kunt praten.", en = "Very easy, Z1; you can talk." }
phases = ["base", "build", "peak", "recovery", "taper"]
load = "easy"
params = { minutes = 25 }
kb_rules = ["TID-1"]

[run_recovery.steps]
work = { hr_zone = [1] }

[run_easy]
name = { nl = "Korte duurloop {minutes}'", en = "Short easy run {minutes}'" }
purpose = { nl = "Aerobe basis, praattempo.", en = "Aerobic base at talking pace." }
cue = { nl = "Rustig, praattempo.", en = "Easy, talking pace." }
phases = ["base", "build", "peak", "recovery"]
load = "easy"
params = { minutes = 40 }
kb_rules = ["TID-1"]

[run_easy.steps]
work = { hr_zone = [2] }

[run_long]
name = { nl = "Lange duurloop {minutes}'", en = "Long run {minutes}'" }
purpose = { nl = "Duurvermogen en vetverbranding.", en = "Endurance and fat use." }
cue = { nl = "Eerste 10' rustig inlopen, daarna gelijkmatig Z2.", en = "First 10' easy, then steady Z2." }
phases = ["base", "build", "peak"]
load = "moderate"
params = { minutes = 90 }
easier = "run_easy"

[run_long.steps]
warmup = { minutes = 10, hr_zone = [1] }
work = { hr_zone = [2] }

[run_brick]
name = { nl = "Brickloop Z2 {minutes}'", en = "Brick run Z2 {minutes}'" }
purpose = { nl = "Lopen op vermoeide benen direct na de rit (PLAN-7).", en = "Running off the bike (PLAN-7)." }
cue = { nl = "Direct na de rit, snel omkleden. Soepel, korte passen.", en = "Straight off the bike. Short, light steps." }
phases = ["build", "peak"]
load = "moderate"
params = { minutes = 20 }
easier = "run_easy"
kb_rules = ["BRICK-1"]

[run_brick.steps]
work = { hr_zone = [2] }

[run_intervals]
name = { nl = "Intervallen {reps}x{work_min}'", en = "Intervals {reps}x{work_min}'" }
purpose = { nl = "Drempel en VO2max.", en = "Threshold and VO2max." }
cue = { nl = "Alleen bij goed herstel; anders duurloop.", en = "Only when recovered; otherwise an easy run." }
phases = ["build", "peak"]
load = "hard"
params = { reps = 4, work_min = 4, rest_min = 2, low = 95, high = 100 }
easier = "run_easy"
kb_rules = ["TID-4", "PLAN-1"]

[run_intervals.steps]
warmup = { minutes = 15, hr_zone = [1, 2] }
work = { target = "lthr_pct" }
recovery = { hr_zone = [1] }
cooldown = { minutes = 10, hr_zone = [1] }

[run_test]
name = { nl = "Looptest {minutes}'", en = "Run test {minutes}'" }
purpose = { nl = "Drempel bepalen: gemiddelde HR van de laatste 20'.", en = "Threshold: average HR of the last 20'." }
cue = { nl = "Uitgerust starten, vlak parcours.", en = "Start rested, flat course." }
phases = ["test"]
load = "hard"
params = { minutes = 30 }

[run_test.steps]
warmup = { minutes = 15, hr_zone = [1, 2] }
work = { cue = "Volle bak, gelijkmatig" }
cooldown = { minutes = 10, hr_zone = [1] }

[ride_endurance]
name = { nl = "Duurrit Z2 {minutes}'", en = "Endurance ride Z2 {minutes}'" }
purpose = { nl = "Aerobe basis op de fiets.", en = "Aerobic base on the bike." }
cue = { nl = "Gelijkmatig, drink elk kwartier.", en = "Steady, drink every 15 minutes." }
phases = ["base", "build", "peak", "recovery"]
load = "easy"
params = { minutes = 60 }

[ride_endurance.steps]
warmup = { minutes = 15, ftp_pct = [50, 65], cadence = [80, 95] }
work = { ftp_pct = [62, 72], cadence = [80, 95], cue = "cadans 80-95 (eigen keuze binnen die band)" }
cooldown = { minutes = 10, ftp_pct = [55] }

[ride_brick]
name = { nl = "Lange rit Z2 {minutes}' (brick)", en = "Long ride Z2 {minutes}' (brick)" }
purpose = { nl = "De rit van een brick: Z2 tot het eind, geen cooling down (BRICK-4).", en = "The ride of a brick: Z2 to the end, no cool-down (BRICK-4)." }
cue = { nl = "Geen cooling down: blijf tot het eind in Z2 en ga direct door naar de brickloop. Drink elk kwartier.", en = "No cool-down: stay in Z2 to the end and go straight into the brick run. Drink every 15 minutes." }
phases = ["base", "build", "peak"]
load = "easy"
params = { minutes = 105 }

[ride_brick.steps]
warmup = { minutes = 15, ftp_pct = [50, 65], cadence = [80, 95] }
work = { ftp_pct = [62, 72], cadence = [80, 95], cue = "cadans 80-95 (eigen keuze binnen die band)" }

[ride_force]
name = { nl = "Kracht op de fiets {reps}x{work_min}'", en = "Force on the bike {reps}x{work_min}'" }
purpose = { nl = "Spierkracht met lage cadans.", en = "Muscular force at low cadence." }
cue = { nl = "Lage cadans, zittend, rustig bovenlichaam.", en = "Low cadence, seated, quiet upper body." }
phases = ["base", "build"]
load = "moderate"
params = { reps = 4, work_min = 5, minutes = 60 }
easier = "ride_endurance"

[ride_force.steps]
warmup = { minutes = 15, ftp_pct = [50, 65], cadence = [90, 90] }
work = { ftp_pct = [75, 85], cadence = [50, 60] }
recovery = { minutes = 3, ftp_pct = [55], cadence = [90, 90] }
cooldown = { min_minutes = 5, ftp_pct = [50, 60], cadence = [90, 90] }

[ride_sweetspot]
name = { nl = "Sweetspot {reps}x{work_min}'", en = "Sweet spot {reps}x{work_min}'" }
purpose = { nl = "Drempelvermogen opbouwen.", en = "Building threshold power." }
cue = { nl = "Alleen bij goed herstel.", en = "Only when recovered." }
phases = ["build", "peak"]
load = "hard"
params = { reps = 3, work_min = 12 }
easier = "ride_endurance"
kb_rules = ["TID-4", "PLAN-1"]

[ride_sweetspot.steps]
warmup = { minutes = 15, ftp_pct = [50, 65], cadence = [90, 90] }
work = { ftp_pct = [88, 93] }
recovery = { minutes = 5, ftp_pct = [55] }
cooldown = { minutes = 10, ftp_pct = [50, 60] }

[ride_ftp_test]
name = { nl = "FTP-test {minutes}'", en = "FTP test {minutes}'" }
purpose = { nl = "FTP = 95% van het gemiddelde vermogen.", en = "FTP = 95% of the average power." }
cue = { nl = "Uitgerust, liefst op Zwift of de trainer.", en = "Rested, ideally on the trainer." }
phases = ["test"]
load = "hard"
params = { minutes = 20 }

[ride_ftp_test.steps]
warmup = { minutes = 20, ftp_pct = [50, 70], cadence = [90, 90] }
opener = { minutes = 5, ftp_pct = [105, 110] }
recovery = { minutes = 10, ftp_pct = [50] }
work = { cue = "Volle bak, gelijkmatig" }
cooldown = { minutes = 15, ftp_pct = [50] }

[mtb]
name = { nl = "MTB {minutes}'", en = "MTB {minutes}'" }
purpose = { nl = "Duur en techniek in het terrein.", en = "Endurance and skills off road." }
cue = { nl = "Stuur op hartslag, niet op vermogen.", en = "Ride by heart rate, not power." }
phases = ["base", "build", "peak"]
load = "moderate"
params = { minutes = 75 }
easier = "ride_endurance"

[mtb.steps]
work = { hr_zone = [1, 2] }

[swim_technique]
name = { nl = "Zwemmen techniek", en = "Swim technique" }
purpose = { nl = "Techniek en waterligging.", en = "Technique and body position." }
cue = { nl = "Licht = RPE 3-4, Middel = RPE 5-6.", en = "Easy = RPE 3-4, Moderate = RPE 5-6." }
phases = ["base", "build", "peak", "recovery"]
load = "easy"
params = { drills = 8, sets = 5, week = 1 }

[swim_technique.steps]
warmup = { metres = 300, pace_zone = [1], cue = "Licht" }
drill = { metres = 50, pace_zone = [2], cue_repeat = 2 }
drill_rest = { seconds = 20 }
hundred = { metres = 100, pace_zone = [2] }
hundred_rest = { seconds = 20 }
cooldown = { metres = 200, pace_zone = [1], cue = "Licht" }

[swim_endurance]
name = { nl = "Zwemmen duur", en = "Swim endurance" }
purpose = { nl = "Aerobe zwemduur.", en = "Aerobic swim endurance." }
cue = { nl = "Middel = RPE 5-6, gelijkmatig.", en = "Moderate = RPE 5-6, steady." }
phases = ["base", "build", "peak"]
load = "moderate"
params = { reps = 4, metres = 300 }
easier = "swim_technique"

[swim_endurance.steps]
warmup = { metres = 300, pace_zone = [1], cue = "Licht" }
work = { pace_zone = [2], cue = "Middel" }
rest = { seconds = 30 }
cooldown = { metres = 200, pace_zone = [1], cue = "Licht" }

[swim_css_test]
name = { nl = "CSS-test 400/200", en = "CSS test 400/200" }
purpose = { nl = "CSS = (400 m - 200 m) / (t400 - t200).", en = "CSS = (400 m - 200 m) / (t400 - t200)." }
cue = { nl = "Beide stukken voluit, rust 5'.", en = "Both efforts all out, 5' rest." }
phases = ["test"]
load = "hard"
params = {}

[swim_css_test.steps]
warmup = { metres = 400, pace_zone = [1], cue = "Licht" }
long_effort = { metres = 400, cue = "Zwaar 400 voluit" }
rest = { minutes = 5 }
short_effort = { metres = 200, cue = "Zwaar 200 voluit" }
cooldown = { metres = 200, pace_zone = [1], cue = "Licht" }

[strength_aa]
name = { nl = "Kracht AA {minutes}'", en = "Strength AA {minutes}'" }
purpose = { nl = "Anatomische adaptatie: pezen en techniek.", en = "Anatomical adaptation: tendons and technique." }
cue = { nl = "Licht gewicht, techniek eerst, nooit tot falen.", en = "Light weights, technique first, never to failure." }
phases = ["base"]
load = "moderate"
params = { sets = 2, reps = 15, minutes = 40 }
kb_rules = ["STR-1"]
exercises = [
  { code = "SQUAT/GOBLET_SQUAT", sets = "sets", reps = "reps", rest_s = 75, label = "Goblet squat" },
  { code = "SQUAT/ALTERNATING_BOX_DUMBBELL_STEP_UPS", sets = "sets", reps = "reps", rest_s = 75, label = "Step-ups kniehoogte" },
  { code = "DEADLIFT/ROMANIAN_DEADLIFT", sets = "sets", reps = "reps", rest_s = 75, label = "Roemeense deadlift" },
  { code = "HIP_RAISE/SINGLE_LEG_HIP_RAISE", sets = 4, reps = "reps", rest_s = 20, label = "Heupbrug op een been" },
  { code = "PULL_UP/LAT_PULLDOWN", sets = "sets", reps = "reps", rest_s = 75, label = "Lat pulldown" },
  { code = "CALF_RAISE/STANDING_CALF_RAISE", sets = "sets", reps = 20, rest_s = 45, label = "Kuitheffing, gestrekte knie" },
  { code = "HIP_STABILITY/DEAD_BUG", sets = 2, reps = 10, rest_s = 30, label = "Dead bug" },
  { code = "PLANK/SIDE_PLANK", sets = 3, time = "30s", side = "both", rest_s = 30, label = "Zijplank" },
]

[strength_aa.steps]
warmup = { minutes = 5, cue = "5' rustig fietsen of roeien" }

[strength_mt_a]
name = { nl = "Kracht MT-A {minutes}'", en = "Strength MT-A {minutes}'" }
purpose = { nl = "Maximale transitie, squat-focus.", en = "Maximum transition, squat focus." }
cue = { nl = "RPE 7-8, nog 1-3 herhalingen over, nooit tot falen.", en = "RPE 7-8, 1-3 reps in reserve, never to failure." }
phases = ["base", "build"]
load = "hard"
params = { sets = 3, reps = 10, minutes = 55 }
easier = "strength_sm"
kb_rules = ["STR-1"]
# De derde beenoefening na squat en RDL: hooguit 8 per been, elk been een eigen stap zodat het
# horloge per been telt (3x10 per been te veel, 3x8 met 20 kg goed).
exercises = [
  { code = "CORE/INCHWORM", sets = 1, reps = 6, rest_s = 30, label = "Inchworm" },
  { code = "SQUAT/BARBELL_BACK_SQUAT", sets = "sets", reps = "reps", rest_s = 120, label = "Back squat" },
  { code = "DEADLIFT/ROMANIAN_DEADLIFT", sets = "sets", reps = "reps", rest_s = 120, label = "Roemeense deadlift" },
  { code = "LUNGE/BARBELL_BULGARIAN_SPLIT_SQUAT", sets = "sets", reps = "reps", reps_max = 8, side = "both", rest_s = 90, label = "Bulgaarse split squat met stang" },
  { code = "ROW/INVERTED_ROW", sets = "sets", reps = "reps", rest_s = 75, label = "Inverted row" },
  { code = "LEG_RAISE/HANGING_KNEE_RAISE", sets = "sets", reps = 10, rest_s = 45, label = "Hangend knieheffen" },
  { code = "CALF_RAISE/STANDING_BARBELL_CALF_RAISE", sets = "sets", reps = 15, rest_s = 45, label = "Kuitheffing staand" },
  { code = "PLANK/SIDE_PLANK", sets = 2, time = "40s", side = "both", rest_s = 30, label = "Zijplank" },
]

[strength_mt_a.steps]
warmup = { minutes = 5, cue = "5' rustig fietsen of roeien" }

[strength_mt_b]
name = { nl = "Kracht MT-B {minutes}'", en = "Strength MT-B {minutes}'" }
purpose = { nl = "Maximale transitie, heup-focus met de Roemeense deadlift.", en = "Maximum transition, hip focus with the Romanian deadlift." }
cue = { nl = "RPE 6-7, rug neutraal, nooit tot falen.", en = "RPE 6-7, neutral back, never to failure." }
phases = ["base", "build"]
load = "hard"
params = { sets = 3, reps = 8, minutes = 50 }
easier = "strength_sm"
kb_rules = ["STR-1"]
# De Roemeense deadlift, niet de deadlift vanaf de vloer: lichter voor de onderrug.
exercises = [
  { code = "CORE/INCHWORM", sets = 1, reps = 6, rest_s = 30, label = "Inchworm" },
  { code = "DEADLIFT/ROMANIAN_DEADLIFT", sets = "sets", reps = "reps", rest_s = 120, label = "Roemeense deadlift, rug neutraal" },
  { code = "HIP_RAISE/BARBELL_HIP_THRUST_WITH_BENCH", sets = "sets", reps = 12, rest_s = 90, label = "Hip thrust tegen bank" },
  { code = "LUNGE/WALKING_LUNGE", sets = "sets", reps = 20, rest_s = 75, label = "Walking lunge, 10 per been" },
  { code = "PULL_UP/LAT_PULLDOWN", sets = "sets", reps = "reps", rest_s = 75, label = "Lat pulldown" },
  { code = "PUSH_UP/PUSH_UP", sets = "sets", reps = 12, rest_s = 60, label = "Push-up, romp recht" },
  { code = "CALF_RAISE/WEIGHTED_SEATED_CALF_RAISE", sets = "sets", reps = 15, rest_s = 45, label = "Kuitheffing zittend" },
  { code = "CORE/RUSSIAN_TWIST", sets = 2, reps = 20, rest_s = 30, label = "Russian twist" },
]

[strength_mt_b.steps]
warmup = { minutes = 5, cue = "5' rustig fietsen of roeien" }

[strength_ms]
name = { nl = "Kracht MS {minutes}'", en = "Strength MS {minutes}'" }
purpose = { nl = "Maximale kracht: zwaar en weinig herhalingen.", en = "Maximum strength: heavy, few reps." }
cue = { nl = "Lange rust, perfecte techniek.", en = "Long rests, perfect technique." }
phases = ["build"]
load = "hard"
params = { sets = 4, reps = 5, minutes = 50 }
easier = "strength_sm"
kb_rules = ["STR-1"]
exercises = [
  { code = "SQUAT/BARBELL_BACK_SQUAT", sets = "sets", reps = "reps", rest_s = 180, label = "Back squat, zwaar" },
  { code = "DEADLIFT/ROMANIAN_DEADLIFT", sets = "sets", reps = "reps", rest_s = 180, label = "Roemeense deadlift, zwaar, rug neutraal" },
  { code = "PULL_UP/LAT_PULLDOWN", sets = "sets", reps = 8, rest_s = 90, label = "Lat pulldown" },
  { code = "CALF_RAISE/STANDING_BARBELL_CALF_RAISE", sets = "sets", reps = 10, rest_s = 60, label = "Kuitheffing staand" },
  { code = "PLANK/SIDE_PLANK", sets = 2, time = "40s", side = "both", rest_s = 30, label = "Zijplank" },
]

[strength_ms.steps]
warmup = { minutes = 5, cue = "5' rustig fietsen of roeien" }

[strength_sm]
name = { nl = "Kracht SM {minutes}'", en = "Strength SM {minutes}'" }
purpose = { nl = "Onderhoud van kracht in de wedstrijdfase.", en = "Strength maintenance in race season." }
cue = { nl = "Kort en fris, geen spierpijn.", en = "Short and fresh, no soreness." }
phases = ["build", "peak"]
load = "moderate"
params = { sets = 2, reps = 8, minutes = 35 }
kb_rules = ["STR-1"]
exercises = [
  { code = "SQUAT/BARBELL_BACK_SQUAT", sets = "sets", reps = "reps", rest_s = 120, label = "Back squat, onderhoud" },
  { code = "DEADLIFT/ROMANIAN_DEADLIFT", sets = "sets", reps = "reps", rest_s = 120, label = "Roemeense deadlift" },
  { code = "HIP_RAISE/SINGLE_LEG_HIP_RAISE", sets = 2, reps = 12, rest_s = 20, label = "Heupbrug op een been" },
  { code = "CALF_RAISE/STANDING_CALF_RAISE", sets = "sets", reps = 15, rest_s = 45, label = "Kuitheffing" },
  { code = "PLANK/SIDE_PLANK", sets = 2, time = "30s", side = "both", rest_s = 30, label = "Zijplank" },
]

[strength_sm.steps]
warmup = { minutes = 5, cue = "5' rustig fietsen of roeien" }
```
