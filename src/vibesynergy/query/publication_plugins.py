"""Independent, bounded enrichment for public activity descriptions.

Quotes are drawn only from a source-checked public-domain pool. A catalogue theme with no
checked line stays unavailable; nothing is written to fill it. Weather is Garmin's own
activity weather, or else the historical reanalysis for the activity's starting position and
hour, never today's conditions. Indoor and virtual activities (Zwift, MyWhoosh, treadmill,
pool, gym) get no weather line: their position is a virtual world or a building. Weather and
quote are on by default and can be switched off in the configuration; the latest successful
section is retained when an enabled plugin is temporarily unavailable. The coach's public
insight line comes from ``publication_insight``, not from a plugin.
"""

from __future__ import annotations

import hashlib
import math
from datetime import UTC, date, datetime, timedelta

import requests
from sqlalchemy import select

from vibesynergy.core.config import load_settings
from vibesynergy.query import publication_format, views
from vibesynergy.store.schema import activity, activity_unified, publication_enrichment

VERSION = 2
PLUGINS = ("weather", "quote")
THEMES = (
    "nature",
    "adventure",
    "curiosity",
    "science",
    "creativity",
    "music",
    "literature",
    "philosophy",
    "humour",
    "friendship",
    "kindness",
    "courage",
    "patience",
    "perseverance",
    "balance",
    "change",
    "freedom",
    "simplicity",
    "gratitude",
    "time",
    "sport",
)
# Where the quotes come from (any source but religious, each one switchable in
# Settings → Sync). Lines without a "collection" are writers and poets, except the authors
# below.
COLLECTIONS = ("writers", "stoics", "science", "explorers", "maxims")
_AUTHOR_COLLECTION = {
    "Marcus Aurelius": "stoics",
    "Juvenal": "stoics",
    "Charles Darwin": "science",
    "Benjamin Franklin": "science",
    "Adam Smith": "science",
}


def collection_of(quote: dict) -> str:
    return quote.get("collection") or _AUTHOR_COLLECTION.get(quote["author"], "writers")


QUOTES = (
    {
        "theme": "nature",
        "text": "I went to the woods because I wished to live deliberately.",
        "author": "Henry David Thoreau",
        "work": "Walden",
        "url": "https://www.gutenberg.org/files/205/205-h/205-h.htm",
    },
    {
        "theme": "simplicity",
        "text": "Simplify, simplify.",
        "author": "Henry David Thoreau",
        "work": "Walden",
        "url": "https://www.gutenberg.org/files/205/205-h/205-h.htm",
    },
    {
        "theme": "philosophy",
        "text": "This above all: to thine own self be true.",
        "author": "William Shakespeare",
        "work": "Hamlet",
        "url": "https://www.gutenberg.org/files/1524/1524-h/1524-h.htm",
    },
    {
        "theme": "courage",
        "text": (
            "Our doubts are traitors, and make us lose the good we oft might win "
            "by fearing to attempt."
        ),
        "author": "William Shakespeare",
        "work": "Measure for Measure",
        "url": "https://www.gutenberg.org/cache/epub/1792/pg1792.html",
    },
    {
        "theme": "freedom",
        "text": "I am no bird; and no net ensnares me.",
        "author": "Charlotte Brontë",
        "work": "Jane Eyre",
        "url": "https://www.gutenberg.org/files/1260/1260-h/1260-h.htm",
    },
    {
        "theme": "gratitude",
        "text": "I can no other answer make but thanks, and thanks, and ever thanks.",
        "author": "William Shakespeare",
        "work": "Twelfth Night",
        "url": "https://www.gutenberg.org/cache/epub/1527/pg1527.html",
    },
    {
        "theme": "adventure",
        "text": "For my part, I travel not to go anywhere, but to go.",
        "author": "the athlete Louis Stevenson",
        "work": "Travels with a Donkey in the Cevennes",
        "url": "https://www.gutenberg.org/files/535/535-h/535-h.htm",
    },
    {
        "theme": "science",
        "text": "Science is the great antidote to the poison of enthusiasm and superstition.",
        "author": "Adam Smith",
        "work": "The Wealth of Nations",
        "url": "https://www.gutenberg.org/files/3300/3300-h/3300-h.htm",
    },
    {
        "theme": "creativity",
        "text": "What is now proved was once only imagined.",
        "author": "William Blake",
        "work": "The Marriage of Heaven and Hell",
        "url": "https://www.gutenberg.org/cache/epub/45315/pg45315.txt",
    },
    {
        "theme": "literature",
        "text": (
            "Some books are to be tasted, others to be swallowed, and some few "
            "to be chewed and digested."
        ),
        "author": "Francis Bacon",
        "work": "Essays",
        "url": "https://www.gutenberg.org/files/575/575-h/575-h.htm",
    },
    {
        "theme": "humour",
        "text": (
            "The secret source of Humor itself is not joy but sorrow. There is no humor in heaven."
        ),
        "author": "Mark Twain",
        "work": "Following the Equator",
        "url": "https://www.gutenberg.org/files/2895/2895-h/2895-h.htm",
    },
    {
        "theme": "friendship",
        "text": "A friend should bear his friend's infirmities.",
        "author": "William Shakespeare",
        "work": "Julius Caesar",
        "url": "https://www.gutenberg.org/files/100/100-0.txt",
    },
    {
        "theme": "kindness",
        "text": "Kind hearts are more than coronets.",
        "author": "Alfred, Lord Tennyson",
        "work": "Lady Clara Vere de Vere",
        "url": "https://www.gutenberg.org/files/8601/8601-h/8601-h.htm",
    },
    {
        "theme": "patience",
        "text": "All things come round to him who will but wait.",
        "author": "Henry Wadsworth Longfellow",
        "work": "Tales of a Wayside Inn",
        "url": "https://www.gutenberg.org/files/25153/25153-h/25153-h.htm",
    },
    {
        "theme": "change",
        "text": "This world is mere change, and this life, opinion.",
        "author": "Marcus Aurelius",
        "work": "Meditations",
        "url": "https://www.gutenberg.org/cache/epub/2680/pg2680.txt",
    },
    {
        "theme": "time",
        "text": (
            "But dost thou love life, then do not squander time, "
            "for that is the stuff life is made of."
        ),
        "author": "Benjamin Franklin",
        "work": "The Way to Wealth",
        "url": "https://www.gutenberg.org/cache/epub/40236/pg40236.txt",
    },
    {
        "theme": "sport",
        "text": "Your prayer must be that you may have a sound mind in a sound body.",
        "author": "Juvenal",
        "work": "Satire X",
        "url": "https://www.gutenberg.org/files/50657/50657-h/50657-h.htm",
    },
    {
        "theme": "nature",
        "text": "Heaven is under our feet as well as over our heads.",
        "author": "Henry David Thoreau",
        "work": "Walden",
        "url": "https://www.gutenberg.org/cache/epub/205/pg205.txt",
    },
    {
        "theme": "nature",
        "text": "In wildness is the preservation of the world.",
        "author": "Henry David Thoreau",
        "work": "Walking",
        "url": "https://www.gutenberg.org/cache/epub/1022/pg1022.txt",
    },
    {
        "theme": "nature",
        "text": "In the woods, we return to reason and faith.",
        "author": "Ralph Waldo Emerson",
        "work": "Nature",
        "url": "https://www.gutenberg.org/cache/epub/29433/pg29433.txt",
    },
    {
        "theme": "nature",
        "text": "Come forth into the light of things, let Nature be your teacher.",
        "author": "William Wordsworth",
        "work": "The Tables Turned",
        "url": "https://www.gutenberg.org/cache/epub/9622/pg9622.txt",
    },
    {
        "theme": "nature",
        "text": "There is a pleasure in the pathless woods.",
        "author": "Lord Byron",
        "work": "Childe Harold's Pilgrimage",
        "url": "https://www.gutenberg.org/cache/epub/5131/pg5131.txt",
    },
    {
        "theme": "simplicity",
        "text": "Our life is frittered away by detail.",
        "author": "Henry David Thoreau",
        "work": "Walden",
        "url": "https://www.gutenberg.org/cache/epub/205/pg205.txt",
    },
    {
        "theme": "simplicity",
        "text": "Brevity is the soul of wit.",
        "author": "William Shakespeare",
        "work": "Hamlet",
        "url": "https://www.gutenberg.org/cache/epub/100/pg100.txt",
    },
    {
        "theme": "philosophy",
        "text": "There is nothing either good or bad, but thinking makes it so.",
        "author": "William Shakespeare",
        "work": "Hamlet",
        "url": "https://www.gutenberg.org/cache/epub/100/pg100.txt",
    },
    {
        "theme": "philosophy",
        "text": "We know what we are, but know not what we may be.",
        "author": "William Shakespeare",
        "work": "Hamlet",
        "url": "https://www.gutenberg.org/cache/epub/100/pg100.txt",
    },
    {
        "theme": "philosophy",
        "text": "Nothing can bring you peace but yourself.",
        "author": "Ralph Waldo Emerson",
        "work": "Self-Reliance",
        "url": "https://www.gutenberg.org/cache/epub/2944/pg2944.txt",
    },
    {
        "theme": "courage",
        "text": "Courage is resistance to fear, mastery of fear, not absence of fear.",
        "author": "Mark Twain",
        "work": "Pudd'nhead Wilson",
        "url": "https://www.gutenberg.org/cache/epub/102/pg102.txt",
    },
    {
        "theme": "courage",
        "text": "Trust thyself: every heart vibrates to that iron string.",
        "author": "Ralph Waldo Emerson",
        "work": "Self-Reliance",
        "url": "https://www.gutenberg.org/cache/epub/2944/pg2944.txt",
    },
    {
        "theme": "freedom",
        "text": (
            "Afoot and light-hearted I take to the open road, healthy, free, the world before me."
        ),
        "author": "Walt Whitman",
        "work": "Song of the Open Road",
        "url": "https://www.gutenberg.org/cache/epub/1322/pg1322.txt",
    },
    {
        "theme": "adventure",
        "text": "To travel hopefully is a better thing than to arrive.",
        "author": "the athlete Louis Stevenson",
        "work": "Virginibus Puerisque",
        "url": "https://www.gutenberg.org/cache/epub/386/pg386.txt",
    },
    {
        "theme": "adventure",
        "text": "An adventure is only an inconvenience rightly considered.",
        "author": "G. K. Chesterton",
        "work": "All Things Considered",
        "url": "https://www.gutenberg.org/cache/epub/11505/pg11505.txt",
    },
    {
        "theme": "science",
        "text": "There is grandeur in this view of life.",
        "author": "Charles Darwin",
        "work": "On the Origin of Species",
        "url": "https://www.gutenberg.org/cache/epub/1228/pg1228.txt",
    },
    {
        "theme": "creativity",
        "text": "A thing of beauty is a joy for ever.",
        "author": "John Keats",
        "work": "Endymion",
        "url": "https://www.gutenberg.org/cache/epub/24280/pg24280.txt",
    },
    {
        "theme": "creativity",
        "text": "We are all in the gutter, but some of us are looking at the stars.",
        "author": "Oscar Wilde",
        "work": "Lady Windermere's Fan",
        "url": "https://www.gutenberg.org/cache/epub/790/pg790.txt",
    },
    {
        "theme": "literature",
        "text": "I declare after all there is no enjoyment like reading!",
        "author": "Jane Austen",
        "work": "Pride and Prejudice",
        "url": "https://www.gutenberg.org/cache/epub/1342/pg1342.txt",
    },
    {
        "theme": "literature",
        "text": "A good book is the precious life-blood of a master spirit.",
        "author": "John Milton",
        "work": "Areopagitica",
        "url": "https://www.gutenberg.org/cache/epub/608/pg608.txt",
    },
    {
        "theme": "humour",
        "text": "I like work: it fascinates me. I can sit and look at it for hours.",
        "author": "Jerome K. Jerome",
        "work": "Three Men in a Boat",
        "url": "https://www.gutenberg.org/cache/epub/308/pg308.txt",
    },
    {
        "theme": "humour",
        "text": "To love oneself is the beginning of a lifelong romance.",
        "author": "Oscar Wilde",
        "work": "An Ideal Husband",
        "url": "https://www.gutenberg.org/cache/epub/885/pg885.txt",
    },
    {
        "theme": "humour",
        "text": (
            "Let us endeavor so to live that when we come to die even the undertaker will be sorry."
        ),
        "author": "Mark Twain",
        "work": "Pudd'nhead Wilson",
        "url": "https://www.gutenberg.org/cache/epub/102/pg102.txt",
    },
    {
        "theme": "friendship",
        "text": "The only way to have a friend is to be one.",
        "author": "Ralph Waldo Emerson",
        "work": "Friendship",
        "url": "https://www.gutenberg.org/cache/epub/2944/pg2944.txt",
    },
    {
        "theme": "friendship",
        "text": "A friend is a person with whom I may be sincere.",
        "author": "Ralph Waldo Emerson",
        "work": "Friendship",
        "url": "https://www.gutenberg.org/cache/epub/2944/pg2944.txt",
    },
    {
        "theme": "kindness",
        "text": (
            "That best portion of a good man's life, his little, nameless, unremembered acts of "
            "kindness and of love."
        ),
        "author": "William Wordsworth",
        "work": "Lines Written a Few Miles above Tintern Abbey",
        "url": "https://www.gutenberg.org/cache/epub/9622/pg9622.txt",
    },
    {
        "theme": "kindness",
        "text": (
            "Have a heart that never hardens, and a temper that never tires, and a touch that "
            "never hurts."
        ),
        "author": "Charles Dickens",
        "work": "Our Mutual Friend",
        "url": "https://www.gutenberg.org/cache/epub/883/pg883.txt",
    },
    {
        "theme": "patience",
        "text": "How poor are they that have not patience!",
        "author": "William Shakespeare",
        "work": "Othello",
        "url": "https://www.gutenberg.org/cache/epub/100/pg100.txt",
    },
    {
        "theme": "patience",
        "text": "They also serve who only stand and wait.",
        "author": "John Milton",
        "work": "On His Blindness",
        "url": "https://www.gutenberg.org/cache/epub/1745/pg1745.txt",
    },
    {
        "theme": "perseverance",
        "text": "To strive, to seek, to find, and not to yield.",
        "author": "Alfred, Lord Tennyson",
        "work": "Ulysses",
        "url": "https://www.gutenberg.org/cache/epub/8601/pg8601.txt",
    },
    {
        "theme": "perseverance",
        "text": "Much rain wears the marble.",
        "author": "William Shakespeare",
        "work": "Henry VI, Part 3",
        "url": "https://www.gutenberg.org/cache/epub/100/pg100.txt",
    },
    {
        "theme": "perseverance",
        "text": (
            "Let us, then, be up and doing, with a heart for any fate; still achieving, still "
            "pursuing, learn to labor and to wait."
        ),
        "author": "Henry Wadsworth Longfellow",
        "work": "A Psalm of Life",
        "url": "https://www.gutenberg.org/cache/epub/1365/pg1365.txt",
    },
    {
        "theme": "perseverance",
        "text": "Nothing great was ever achieved without enthusiasm.",
        "author": "Ralph Waldo Emerson",
        "work": "Circles",
        "url": "https://www.gutenberg.org/cache/epub/2944/pg2944.txt",
    },
    {
        "theme": "change",
        "text": (
            "Habit is habit, and not to be flung out of the window by any man, but coaxed down-"
            "stairs a step at a time."
        ),
        "author": "Mark Twain",
        "work": "Pudd'nhead Wilson",
        "url": "https://www.gutenberg.org/cache/epub/102/pg102.txt",
    },
    {
        "theme": "time",
        "text": "Lost time is never found again.",
        "author": "Benjamin Franklin",
        "work": "Poor Richard's Almanack",
        "url": "https://www.gutenberg.org/cache/epub/40236/pg40236.txt",
    },
    {
        "theme": "time",
        "text": "Make use of time, let not advantage slip.",
        "author": "William Shakespeare",
        "work": "Venus and Adonis",
        "url": "https://www.gutenberg.org/cache/epub/100/pg100.txt",
    },
    {
        "theme": "sport",
        "text": (
            "Now, here, you see, it takes all the running you can do, to keep in the same place."
        ),
        "author": "Lewis Carroll",
        "work": "Through the Looking-Glass",
        "url": "https://www.gutenberg.org/cache/epub/12/pg12.txt",
    },
    {
        "theme": "sport",
        "text": "If you can fill the unforgiving minute with sixty seconds' worth of distance run.",
        "author": "Rudyard Kipling",
        "work": "If—",
        "url": "https://www.gutenberg.org/cache/epub/556/pg556.txt",
    },
    {
        "theme": "curiosity",
        "text": "Curiouser and curiouser!",
        "author": "Lewis Carroll",
        "work": "Alice's Adventures in Wonderland",
        "url": "https://www.gutenberg.org/cache/epub/11/pg11.txt",
    },
    {
        "theme": "music",
        "text": "If music be the food of love, play on.",
        "author": "William Shakespeare",
        "work": "Twelfth Night",
        "url": "https://www.gutenberg.org/cache/epub/100/pg100.txt",
    },
    {
        "collection": "stoics",
        "theme": "patience",
        "text": (
            "Demand not that events should happen as you wish; but wish them to happen as they do"
            " happen, and you will go on well."
        ),
        "author": "Epictetus",
        "work": "The Enchiridion",
        "url": "https://www.gutenberg.org/cache/epub/45109/pg45109.txt",
    },
    {
        "collection": "stoics",
        "theme": "courage",
        "text": "Sickness is an impediment to the body, but not to the will unless itself pleases.",
        "author": "Epictetus",
        "work": "The Enchiridion",
        "url": "https://www.gutenberg.org/cache/epub/45109/pg45109.txt",
    },
    {
        "collection": "stoics",
        "theme": "perseverance",
        "text": "Begin therefore with little things.",
        "author": "Epictetus",
        "work": "The Enchiridion",
        "url": "https://www.gutenberg.org/cache/epub/45109/pg45109.txt",
    },
    {
        "collection": "stoics",
        "theme": "philosophy",
        "text": (
            "If you would improve, be content to be thought foolish and dull with regard to "
            "externals."
        ),
        "author": "Epictetus",
        "work": "The Enchiridion",
        "url": "https://www.gutenberg.org/cache/epub/45109/pg45109.txt",
    },
    {
        "collection": "stoics",
        "theme": "sport",
        "text": "Exercise, therefore, what is in your power.",
        "author": "Epictetus",
        "work": "The Enchiridion",
        "url": "https://www.gutenberg.org/cache/epub/45109/pg45109.txt",
    },
    {
        "collection": "stoics",
        "theme": "patience",
        "text": (
            "When, therefore, anyone provokes you, be assured that it is your own opinion which "
            "provokes you."
        ),
        "author": "Epictetus",
        "work": "The Enchiridion",
        "url": "https://www.gutenberg.org/cache/epub/45109/pg45109.txt",
    },
    {
        "collection": "stoics",
        "theme": "philosophy",
        "text": "Try, therefore, in the first place, not to be bewildered by appearances.",
        "author": "Epictetus",
        "work": "The Enchiridion",
        "url": "https://www.gutenberg.org/cache/epub/45109/pg45109.txt",
    },
    {
        "collection": "stoics",
        "theme": "simplicity",
        "text": "It is enough if everyone fully performs his own proper business.",
        "author": "Epictetus",
        "work": "The Enchiridion",
        "url": "https://www.gutenberg.org/cache/epub/45109/pg45109.txt",
    },
    {
        "collection": "stoics",
        "theme": "balance",
        "text": (
            "Consider first, man, what the matter is, and what your own nature is able to bear."
        ),
        "author": "Epictetus",
        "work": "The Enchiridion",
        "url": "https://www.gutenberg.org/cache/epub/45109/pg45109.txt",
    },
    {
        "collection": "stoics",
        "theme": "sport",
        "text": (
            "If you would be a wrestler, consider your shoulders, your back, your thighs; for "
            "different persons are made for different things."
        ),
        "author": "Epictetus",
        "work": "The Enchiridion",
        "url": "https://www.gutenberg.org/cache/epub/45109/pg45109.txt",
    },
    {
        "collection": "stoics",
        "theme": "simplicity",
        "text": "Be mostly silent, or speak merely what is needful, and in few words.",
        "author": "Epictetus",
        "work": "The Enchiridion",
        "url": "https://www.gutenberg.org/cache/epub/45109/pg45109.txt",
    },
    {
        "collection": "stoics",
        "theme": "philosophy",
        "text": (
            "Never proclaim yourself a philosopher, nor make much talk among the ignorant about "
            "your principles, but show them by actions."
        ),
        "author": "Epictetus",
        "work": "The Enchiridion",
        "url": "https://www.gutenberg.org/cache/epub/45109/pg45109.txt",
    },
    {
        "collection": "stoics",
        "theme": "courage",
        "text": (
            "When you have reckoned up all this, if your inclination still holds, set about the "
            "combat."
        ),
        "author": "Epictetus",
        "work": "The Enchiridion",
        "url": "https://www.gutenberg.org/cache/epub/45109/pg45109.txt",
    },
    {
        "collection": "stoics",
        "theme": "balance",
        "text": (
            "The body is to everyone the proper measure of its possessions, as the foot is of the"
            " shoe."
        ),
        "author": "Epictetus",
        "work": "The Enchiridion",
        "url": "https://www.gutenberg.org/cache/epub/45109/pg45109.txt",
    },
    {
        "collection": "stoics",
        "theme": "curiosity",
        "text": (
            "Give thyself leisure to learn some good thing, and cease roving and wandering to and"
            " fro."
        ),
        "author": "Marcus Aurelius",
        "work": "Meditations (tr. Meric Casaubon)",
        "url": "https://www.gutenberg.org/cache/epub/2680/pg2680.txt",
    },
    {
        "collection": "stoics",
        "theme": "simplicity",
        "text": "Be neither a great talker, nor a great undertaker.",
        "author": "Marcus Aurelius",
        "work": "Meditations (tr. Meric Casaubon)",
        "url": "https://www.gutenberg.org/cache/epub/2680/pg2680.txt",
    },
    {
        "collection": "stoics",
        "theme": "perseverance",
        "text": (
            "Do thou therefore I say absolutely and freely make choice of that which is best, and"
            " stick unto it."
        ),
        "author": "Marcus Aurelius",
        "work": "Meditations (tr. Meric Casaubon)",
        "url": "https://www.gutenberg.org/cache/epub/2680/pg2680.txt",
    },
    {
        "collection": "stoics",
        "theme": "patience",
        "text": (
            "Let nothing be done rashly, and at random, but all things according to the most "
            "exact and perfect rules of art."
        ),
        "author": "Marcus Aurelius",
        "work": "Meditations (tr. Meric Casaubon)",
        "url": "https://www.gutenberg.org/cache/epub/2680/pg2680.txt",
    },
    {
        "collection": "stoics",
        "theme": "balance",
        "text": (
            "At what time soever thou wilt, it is in thy power to retire into thyself, and to be "
            "at rest, and free from all businesses."
        ),
        "author": "Marcus Aurelius",
        "work": "Meditations (tr. Meric Casaubon)",
        "url": "https://www.gutenberg.org/cache/epub/2680/pg2680.txt",
    },
    {
        "collection": "stoics",
        "theme": "balance",
        "text": (
            "Afford then thyself this retiring continually, and thereby refresh and renew thyself."
        ),
        "author": "Marcus Aurelius",
        "work": "Meditations (tr. Meric Casaubon)",
        "url": "https://www.gutenberg.org/cache/epub/2680/pg2680.txt",
    },
    {
        "collection": "stoics",
        "theme": "time",
        "text": "Not as though thou hadst thousands of years to live.",
        "author": "Marcus Aurelius",
        "work": "Meditations (tr. Meric Casaubon)",
        "url": "https://www.gutenberg.org/cache/epub/2680/pg2680.txt",
    },
    {
        "collection": "stoics",
        "theme": "simplicity",
        "text": "Trouble not thyself any more henceforth, reduce thyself unto perfect simplicity.",
        "author": "Marcus Aurelius",
        "work": "Meditations (tr. Meric Casaubon)",
        "url": "https://www.gutenberg.org/cache/epub/2680/pg2680.txt",
    },
    {
        "collection": "stoics",
        "theme": "balance",
        "text": "Use recreation with sobriety.",
        "author": "Marcus Aurelius",
        "work": "Meditations (tr. Meric Casaubon)",
        "url": "https://www.gutenberg.org/cache/epub/2680/pg2680.txt",
    },
    {
        "collection": "stoics",
        "theme": "simplicity",
        "text": "Let thy course ever be the most compendious way.",
        "author": "Marcus Aurelius",
        "work": "Meditations (tr. Meric Casaubon)",
        "url": "https://www.gutenberg.org/cache/epub/2680/pg2680.txt",
    },
    {
        "collection": "stoics",
        "theme": "gratitude",
        "text": "Wheresoever thou mayest live, there it is in thy power to live well and happy.",
        "author": "Marcus Aurelius",
        "work": "Meditations (tr. Meric Casaubon)",
        "url": "https://www.gutenberg.org/cache/epub/2680/pg2680.txt",
    },
    {
        "collection": "stoics",
        "theme": "kindness",
        "text": "The best kind of revenge is, not to become like unto them.",
        "author": "Marcus Aurelius",
        "work": "Meditations (tr. Meric Casaubon)",
        "url": "https://www.gutenberg.org/cache/epub/2680/pg2680.txt",
    },
    {
        "collection": "stoics",
        "theme": "sport",
        "text": (
            "Was it not in very truth for this, that thou mightest always be busy and in action?"
        ),
        "author": "Marcus Aurelius",
        "work": "Meditations (tr. Meric Casaubon)",
        "url": "https://www.gutenberg.org/cache/epub/2680/pg2680.txt",
    },
    {
        "collection": "stoics",
        "theme": "change",
        "text": "Some things hasten to be, and others to be no more.",
        "author": "Marcus Aurelius",
        "work": "Meditations (tr. Meric Casaubon)",
        "url": "https://www.gutenberg.org/cache/epub/2680/pg2680.txt",
    },
    {
        "collection": "science",
        "theme": "adventure",
        "text": (
            "Every one must know the feeling of triumph and pride which a grand view from a "
            "height communicates to the mind."
        ),
        "author": "Charles Darwin",
        "work": "The Voyage of the Beagle",
        "url": "https://www.gutenberg.org/cache/epub/944/pg944.txt",
    },
    {
        "collection": "science",
        "theme": "adventure",
        "text": (
            "My excursion only cost me twenty-four days, and never did I more deeply enjoy an "
            "equal space of time."
        ),
        "author": "Charles Darwin",
        "work": "The Voyage of the Beagle",
        "url": "https://www.gutenberg.org/cache/epub/944/pg944.txt",
    },
    {
        "collection": "science",
        "theme": "curiosity",
        "text": (
            "How great would be the desire in every admirer of nature to behold, if such were "
            "possible, the scenery of another planet!"
        ),
        "author": "Charles Darwin",
        "work": "The Voyage of the Beagle",
        "url": "https://www.gutenberg.org/cache/epub/944/pg944.txt",
    },
    {
        "collection": "science",
        "theme": "curiosity",
        "text": (
            "Often as I recurred to a branch of the kelp, I never failed to discover animals of "
            "new and curious structures."
        ),
        "author": "Charles Darwin",
        "work": "The Voyage of the Beagle",
        "url": "https://www.gutenberg.org/cache/epub/944/pg944.txt",
    },
    {
        "collection": "science",
        "theme": "nature",
        "text": (
            "The elements of the scenery are so simple, that they are worth mentioning, as a "
            "proof on what trifling circumstances exquisite natural beauty depends."
        ),
        "author": "Charles Darwin",
        "work": "The Voyage of the Beagle",
        "url": "https://www.gutenberg.org/cache/epub/944/pg944.txt",
    },
    {
        "collection": "science",
        "theme": "science",
        "text": (
            "I hope you will now see that the perfection of a process—that is, its utility—is the"
            " better point of beauty about it."
        ),
        "author": "Michael Faraday",
        "work": "The Chemical History of a Candle",
        "url": "https://www.gutenberg.org/cache/epub/14474/pg14474.txt",
    },
    {
        "collection": "science",
        "theme": "gratitude",
        "text": (
            "Human felicity is produced not so much by great pieces of good fortune that seldom "
            "happen, as by little advantages that occur every day."
        ),
        "author": "Benjamin Franklin",
        "work": "Autobiography",
        "url": "https://www.gutenberg.org/cache/epub/20203/pg20203.txt",
    },
    {
        "collection": "explorers",
        "theme": "perseverance",
        "text": (
            "Human effort is not futile, but man fights against the giant forces of Nature in a "
            "spirit of humility."
        ),
        "author": "Ernest Shackleton",
        "work": "South",
        "url": "https://www.gutenberg.org/cache/epub/5199/pg5199.txt",
    },
    {
        "collection": "explorers",
        "theme": "courage",
        "text": (
            "The time for renewed action was coming, and though our situation was grave enough, "
            "we were facing the future hopefully."
        ),
        "author": "Ernest Shackleton",
        "work": "South",
        "url": "https://www.gutenberg.org/cache/epub/5199/pg5199.txt",
    },
    {
        "collection": "explorers",
        "theme": "simplicity",
        "text": "Man can sustain life with very scanty means.",
        "author": "Ernest Shackleton",
        "work": "South",
        "url": "https://www.gutenberg.org/cache/epub/5199/pg5199.txt",
    },
    {
        "collection": "explorers",
        "theme": "friendship",
        "text": "The men always managed to reply cheerfully.",
        "author": "Ernest Shackleton",
        "work": "South",
        "url": "https://www.gutenberg.org/cache/epub/5199/pg5199.txt",
    },
    {
        "collection": "explorers",
        "theme": "perseverance",
        "text": "The men had worked long hours without thought of rest, and they deserved success.",
        "author": "Ernest Shackleton",
        "work": "South",
        "url": "https://www.gutenberg.org/cache/epub/5199/pg5199.txt",
    },
    {
        "collection": "explorers",
        "theme": "patience",
        "text": (
            "The task was likely to be long and strenuous, and an ordered mind and a clear "
            "programme were essential if we were to come through without loss of life."
        ),
        "author": "Ernest Shackleton",
        "work": "South",
        "url": "https://www.gutenberg.org/cache/epub/5199/pg5199.txt",
    },
    {
        "collection": "explorers",
        "theme": "sport",
        "text": (
            "Every mile gained towards the south meant a mile less sledging when the time came "
            "for the overland journey."
        ),
        "author": "Ernest Shackleton",
        "work": "South",
        "url": "https://www.gutenberg.org/cache/epub/5199/pg5199.txt",
    },
    {
        "collection": "explorers",
        "theme": "humour",
        "text": "Evidently life beneath the pack is not always monotonous.",
        "author": "Ernest Shackleton",
        "work": "South",
        "url": "https://www.gutenberg.org/cache/epub/5199/pg5199.txt",
    },
    {
        "collection": "explorers",
        "theme": "perseverance",
        "text": "The great trouble is to get the sledge started after the many unavoidable stops.",
        "author": "Ernest Shackleton",
        "work": "South",
        "url": "https://www.gutenberg.org/cache/epub/5199/pg5199.txt",
    },
    {
        "collection": "explorers",
        "theme": "nature",
        "text": "Beauty beyond thought everywhere, beneath, above, made and being made forever.",
        "author": "John Muir",
        "work": "My First Summer in the Sierra",
        "url": "https://www.gutenberg.org/cache/epub/32540/pg32540.txt",
    },
    {
        "collection": "explorers",
        "theme": "time",
        "text": (
            "Life seems neither long nor short, and we take no more heed to save time or make "
            "haste than do the trees and stars."
        ),
        "author": "John Muir",
        "work": "My First Summer in the Sierra",
        "url": "https://www.gutenberg.org/cache/epub/32540/pg32540.txt",
    },
    {
        "collection": "explorers",
        "theme": "freedom",
        "text": (
            "Only spread a fern frond over a man's head and worldly cares are cast out, and "
            "freedom and beauty and peace come in."
        ),
        "author": "John Muir",
        "work": "My First Summer in the Sierra",
        "url": "https://www.gutenberg.org/cache/epub/32540/pg32540.txt",
    },
    {
        "collection": "explorers",
        "theme": "nature",
        "text": (
            "How many mouths Nature has to fill, how many neighbors we have, how little we know "
            "about them, and how seldom we get in each other's way!"
        ),
        "author": "John Muir",
        "work": "My First Summer in the Sierra",
        "url": "https://www.gutenberg.org/cache/epub/32540/pg32540.txt",
    },
    {
        "collection": "explorers",
        "theme": "nature",
        "text": (
            "Down the long mountain-slopes the sunbeams pour, gilding the awakening pines, "
            "cheering every needle, filling every living thing with joy."
        ),
        "author": "John Muir",
        "work": "My First Summer in the Sierra",
        "url": "https://www.gutenberg.org/cache/epub/32540/pg32540.txt",
    },
    {
        "collection": "explorers",
        "theme": "music",
        "text": "Everything kept in joyful rhythmic motion in the pulses of Nature's big heart.",
        "author": "John Muir",
        "work": "My First Summer in the Sierra",
        "url": "https://www.gutenberg.org/cache/epub/32540/pg32540.txt",
    },
    {
        "collection": "explorers",
        "theme": "curiosity",
        "text": (
            "How vast it seems, how short human life when we happen to think of it, and how "
            "little we may learn, however hard we try!"
        ),
        "author": "John Muir",
        "work": "My First Summer in the Sierra",
        "url": "https://www.gutenberg.org/cache/epub/32540/pg32540.txt",
    },
    {
        "collection": "explorers",
        "theme": "adventure",
        "text": (
            "Never before had I seen so glorious a landscape, so boundless an affluence of "
            "sublime mountain beauty."
        ),
        "author": "John Muir",
        "work": "My First Summer in the Sierra",
        "url": "https://www.gutenberg.org/cache/epub/32540/pg32540.txt",
    },
    {
        "collection": "maxims",
        "theme": "perseverance",
        "text": (
            "We have more strength than will; and it is often merely for an excuse we say things "
            "are impossible."
        ),
        "author": "François de La Rochefoucauld",
        "work": "Reflections; or Sentences and Moral Maxims",
        "url": "https://www.gutenberg.org/cache/epub/9105/pg9105.txt",
    },
    {
        "collection": "maxims",
        "theme": "balance",
        "text": (
            "Those who apply themselves too closely to little things often become incapable of "
            "great things."
        ),
        "author": "François de La Rochefoucauld",
        "work": "Reflections; or Sentences and Moral Maxims",
        "url": "https://www.gutenberg.org/cache/epub/9105/pg9105.txt",
    },
    {
        "collection": "maxims",
        "theme": "balance",
        "text": "We are never so happy or so unhappy as we suppose.",
        "author": "François de La Rochefoucauld",
        "work": "Reflections; or Sentences and Moral Maxims",
        "url": "https://www.gutenberg.org/cache/epub/9105/pg9105.txt",
    },
    {
        "collection": "maxims",
        "theme": "philosophy",
        "text": "What grace is to the body good sense is to the mind.",
        "author": "François de La Rochefoucauld",
        "work": "Reflections; or Sentences and Moral Maxims",
        "url": "https://www.gutenberg.org/cache/epub/9105/pg9105.txt",
    },
    {
        "collection": "maxims",
        "theme": "creativity",
        "text": (
            "Ideas often flash across our minds more complete than we could make them after much "
            "labour."
        ),
        "author": "François de La Rochefoucauld",
        "work": "Reflections; or Sentences and Moral Maxims",
        "url": "https://www.gutenberg.org/cache/epub/9105/pg9105.txt",
    },
    {
        "collection": "maxims",
        "theme": "humour",
        "text": "It is far easier to be wise for others than to be so for oneself.",
        "author": "François de La Rochefoucauld",
        "work": "Reflections; or Sentences and Moral Maxims",
        "url": "https://www.gutenberg.org/cache/epub/9105/pg9105.txt",
    },
    {
        "collection": "maxims",
        "theme": "philosophy",
        "text": "Nature makes merit but fortune sets it to work.",
        "author": "François de La Rochefoucauld",
        "work": "Reflections; or Sentences and Moral Maxims",
        "url": "https://www.gutenberg.org/cache/epub/9105/pg9105.txt",
    },
    {
        "collection": "maxims",
        "theme": "sport",
        "text": (
            "It is not enough to have great qualities, we should also have the management of them."
        ),
        "author": "François de La Rochefoucauld",
        "work": "Reflections; or Sentences and Moral Maxims",
        "url": "https://www.gutenberg.org/cache/epub/9105/pg9105.txt",
    },
    {
        "collection": "maxims",
        "theme": "courage",
        "text": (
            "However deceitful hope may be, yet she carries us on pleasantly to the end of life."
        ),
        "author": "François de La Rochefoucauld",
        "work": "Reflections; or Sentences and Moral Maxims",
        "url": "https://www.gutenberg.org/cache/epub/9105/pg9105.txt",
    },
    {
        "collection": "maxims",
        "theme": "patience",
        "text": "He is really wise who is nettled at nothing.",
        "author": "François de La Rochefoucauld",
        "work": "Reflections; or Sentences and Moral Maxims",
        "url": "https://www.gutenberg.org/cache/epub/9105/pg9105.txt",
    },
    {
        "collection": "maxims",
        "theme": "time",
        "text": "In growing old we become more foolish--and more wise.",
        "author": "François de La Rochefoucauld",
        "work": "Reflections; or Sentences and Moral Maxims",
        "url": "https://www.gutenberg.org/cache/epub/9105/pg9105.txt",
    },
    {
        "collection": "maxims",
        "theme": "change",
        "text": (
            "Absence extinguishes small passions and increases great ones, as the wind will blow "
            "out a candle, and blow in a fire."
        ),
        "author": "François de La Rochefoucauld",
        "work": "Reflections; or Sentences and Moral Maxims",
        "url": "https://www.gutenberg.org/cache/epub/9105/pg9105.txt",
    },
    {
        "collection": "maxims",
        "theme": "change",
        "text": "To be a great man one should know how to profit by every phase of fortune.",
        "author": "François de La Rochefoucauld",
        "work": "Reflections; or Sentences and Moral Maxims",
        "url": "https://www.gutenberg.org/cache/epub/9105/pg9105.txt",
    },
    {
        "collection": "maxims",
        "theme": "patience",
        "text": (
            "Little minds are too much wounded by little things; great minds see all and are not "
            "even hurt."
        ),
        "author": "François de La Rochefoucauld",
        "work": "Reflections; or Sentences and Moral Maxims",
        "url": "https://www.gutenberg.org/cache/epub/9105/pg9105.txt",
    },
    {
        "collection": "maxims",
        "theme": "friendship",
        "text": (
            "The greatest effort of friendship is not to show our faults to a friend, but to show"
            " him his own."
        ),
        "author": "François de La Rochefoucauld",
        "work": "Reflections; or Sentences and Moral Maxims",
        "url": "https://www.gutenberg.org/cache/epub/9105/pg9105.txt",
    },
    {
        "collection": "maxims",
        "theme": "kindness",
        "text": "To praise good actions heartily is in some measure to take part in them.",
        "author": "François de La Rochefoucauld",
        "work": "Reflections; or Sentences and Moral Maxims",
        "url": "https://www.gutenberg.org/cache/epub/9105/pg9105.txt",
    },
    {
        "collection": "maxims",
        "theme": "sport",
        "text": (
            "We should not judge of a man's merit by his great abilities, but by the use he makes"
            " of them."
        ),
        "author": "François de La Rochefoucauld",
        "work": "Reflections; or Sentences and Moral Maxims",
        "url": "https://www.gutenberg.org/cache/epub/9105/pg9105.txt",
    },
    {
        "collection": "maxims",
        "theme": "adventure",
        "text": (
            "In great matters we should not try so much to create opportunities as to utilise "
            "those that offer themselves."
        ),
        "author": "François de La Rochefoucauld",
        "work": "Reflections; or Sentences and Moral Maxims",
        "url": "https://www.gutenberg.org/cache/epub/9105/pg9105.txt",
    },
)
WEATHER_URL = "https://archive-api.open-meteo.com/v1/archive"
# The last days are not in the archive yet; the forecast service keeps them (past_days).
RECENT_WEATHER_URL = "https://api.open-meteo.com/v1/forecast"
INDOOR_SPORTS = frozenset(
    {
        "elliptical",
        "fitness_equipment",
        "indoor_cardio",
        "indoor_climbing",
        "indoor_cycling",
        "indoor_rowing",
        "indoor_running",
        "indoor_walking",
        "lap_swimming",
        "pilates",
        "stair_climbing",
        "strength_training",
        "treadmill_running",
        "virtual_ride",
        "virtual_run",
        "yoga",
    }
)
# Garmin's manufacturer for activities recorded in a virtual world.
VIRTUAL_PLATFORMS = frozenset({"ZWIFT", "MYWHOOSH", "ROUVY", "FULGAZ", "BKOOL", "TRAINERROAD"})


def _indoor_sport(sport) -> bool:
    sport = str(sport or "").lower()
    return sport in INDOOR_SPORTS or any(w in sport for w in ("virtual", "indoor", "treadmill"))


def indoor(rt, activity_id: str) -> bool:
    """True for an indoor or virtual activity, from the sport, Strava's trainer flag or the
    virtual platform Garmin names. Unknown stays outdoor (the weather line needs real data)."""
    group = views.resolve_activity(rt.engine, activity_id)
    with rt.engine.connect() as conn:
        row = (
            conn.execute(
                select(activity_unified.c.sport, activity_unified.c.sources).where(
                    activity_unified.c.group_id == group
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            return False
        if _indoor_sport(row["sport"]):
            return True
        for ref in row["sources"] or []:
            source, _, source_id = str(ref).partition(":")
            if source not in {"garmin", "strava"}:
                continue
            found = conn.execute(
                select(activity.c.sport, activity.c.extra).where(
                    activity.c.source == source, activity.c.source_id == source_id
                )
            ).first()
            if found is None:
                continue
            extra = found.extra if isinstance(found.extra, dict) else {}
            if _indoor_sport(found.sport):
                return True
            if source == "strava" and extra.get("trainer") is True:
                return True
            if str(extra.get("manufacturer") or "").upper() in VIRTUAL_PLATFORMS:
                return True
    return False


def _cached(rt, identity: str, name: str) -> dict | None:
    with rt.engine.connect() as conn:
        row = (
            conn.execute(
                select(publication_enrichment).where(
                    publication_enrichment.c.identity_id == identity,
                    publication_enrichment.c.plugin_id == name,
                )
            )
            .mappings()
            .first()
        )
    return dict(row) if row else None


def _save(rt, identity: str, name: str, section: str, provenance: dict, cache_version: str) -> None:
    with rt.engine.begin() as conn:
        conn.execute(
            publication_enrichment.delete().where(
                publication_enrichment.c.identity_id == identity,
                publication_enrichment.c.plugin_id == name,
            )
        )
        conn.execute(
            publication_enrichment.insert().values(
                identity_id=identity,
                plugin_id=name,
                version=cache_version,
                section=section,
                provenance=provenance,
                updated_at=datetime.now(UTC),
            )
        )


def quote_id_of(item: dict) -> str:
    return hashlib.sha256(item["text"].encode()).hexdigest()[:16]


def used_quote_ids(rt, exclude: str | None = None) -> set[str]:
    """Every quote already used in a published block (all activities, ever)."""
    query = select(publication_enrichment.c.provenance).where(
        publication_enrichment.c.plugin_id == "quote"
    )
    if exclude:
        query = query.where(publication_enrichment.c.identity_id != exclude)
    with rt.engine.connect() as conn:
        rows = conn.execute(query).scalars().all()
    return {p["quote_id"] for p in rows if isinstance(p, dict) and p.get("quote_id")}


# Where quotes come from: the built-in list and free sources, each switchable.
PROVIDERS = ("builtin", "wikiquote", "quotable", "zenquotes")


def _all_quotes(rt, providers: list[str] | None) -> list[dict]:
    """The built-in list (by collection) and the fetched pool, for the chosen providers."""
    from vibesynergy.query import quote_sources

    chosen = set(providers if providers is not None else PROVIDERS)
    out = list(QUOTES) if "builtin" in chosen else []
    pool = quote_sources.as_quotes(quote_sources.load(rt.settings.data_dir))
    return out + [q for q in pool if q["collection"] in chosen]


def quotes_left(
    rt, themes: list[str], sources: list[str] | None = None, providers: list[str] | None = None
) -> dict[str, int]:
    """Never-used quotes per built-in collection and per fetched provider, in these themes."""
    used = used_quote_ids(rt)
    out = dict.fromkeys(list(sources if sources is not None else COLLECTIONS), 0)
    for name in providers if providers is not None else PROVIDERS:
        if name != "builtin":
            out[name] = 0
    for q in _all_quotes(rt, providers):
        source = collection_of(q)
        if source in out and q["theme"] in themes and quote_id_of(q) not in used:
            out[source] += 1
    return out


def _quote(
    rt,
    identity: str,
    language: str,
    themes: list[str],
    sources: list[str] | None = None,
    providers: list[str] | None = None,
) -> tuple[str, dict] | None:
    # Built-in lines follow the collections; fetched ones (collection = provider) always pass.
    wanted = set(sources if sources is not None else COLLECTIONS) | set(PROVIDERS)
    choices = [
        q for q in _all_quotes(rt, providers) if q["theme"] in themes and collection_of(q) in wanted
    ]
    if not choices:
        return None
    # quotes are always unique. A quote used for one activity is never used
    # for another; when every quote in the chosen themes was used, there is no quote line
    # (Settings → Sync shows how many are left). The same activity keeps its quote (_cached).
    used = used_quote_ids(rt, exclude=identity)

    def quote_id(item):
        return hashlib.sha256(item["text"].encode()).hexdigest()[:16]

    offset = int.from_bytes(hashlib.sha256(identity.encode()).digest()[:4], "big") % len(choices)
    rotated = choices[offset:] + choices[:offset]
    unused = [item for item in rotated if quote_id(item) not in used]
    if not unused:
        return None
    q = unused[0]
    # ZenQuotes asks to be named where its quotes appear.
    via = " (via zenquotes.io)" if q.get("collection") == "zenquotes" else ""
    return f"{q['text']} — {q['author']}{via}", {
        "source": q["url"],
        "work": q["work"],
        "theme": q["theme"],
        "quote_id": quote_id(q),
        "collection": collection_of(q),
    }


def _location(rt, activity_id: str) -> tuple[float, float, datetime] | None:
    group = views.resolve_activity(rt.engine, activity_id)
    with rt.engine.connect() as conn:
        unified = (
            conn.execute(select(activity_unified).where(activity_unified.c.group_id == group))
            .mappings()
            .one()
        )
        for ref in unified["sources"]:
            source, source_id = ref.split(":", 1)
            if source not in {"garmin", "strava"}:
                continue
            raw_id = conn.scalar(
                select(activity.c.raw_item_id).where(
                    activity.c.source == source, activity.c.source_id == source_id
                )
            )
            if not raw_id:
                continue
            raw = rt.raw.load(raw_id)
            if raw.content_type != "application/json" or len(raw.payload) > 200_000:
                continue
            data = raw.json() or {}
            summary = data.get("summary") or {}
            detail = data.get("detail") or {}
            point = detail.get("start_latlng") or summary.get("start_latlng")
            if isinstance(point, list) and len(point) == 2:
                lat, lon = point
            else:
                lat = summary.get("startLatitude")
                lon = summary.get("startLongitude")
            if (
                all(isinstance(x, int | float) and math.isfinite(x) for x in (lat, lon))
                and -90 <= lat <= 90
                and -180 <= lon <= 180
                and (lat, lon) != (0, 0)
            ):
                return lat, lon, unified["start_utc"]
    return None


def _weather(
    rt,
    activity_id: str,
    language: str,
    *,
    http=requests.get,
    now: datetime | None = None,
    recent: bool = True,
) -> tuple[str, dict] | None:
    """Garmin's activity weather, else Open-Meteo at the start point and hour. The last five
    days come from the forecast service (the calendar, and since 8-11 the publication:
    a brick run published right after had no weather while it waited for the archive);
    ``recent=False`` waits for the archive."""
    try:
        garmin = publication_format.garmin_fields(rt, activity_id).get("weather")
    except ValueError:
        garmin = None
    units = {"temperature": rt.settings.temperature, "wind": rt.settings.wind}
    if line := publication_format.garmin_weather(garmin, language, **units):
        return line, {
            "source": "garmin activity weather",
            "time": garmin.get("issueDate") if isinstance(garmin.get("issueDate"), str) else None,
            "method": "nearest weather station reported by Garmin",
        }
    found = _location(rt, activity_id)
    if found is None:
        return None
    lat, lon, start = found
    url = WEATHER_URL
    if start > (now or datetime.now(UTC)) - timedelta(days=5):
        if not recent:
            return None  # historical archive may not yet hold this hour
        url = RECENT_WEATHER_URL
    params = {
        "latitude": round(lat, 3),
        "longitude": round(lon, 3),
        "start_date": start.date().isoformat(),
        "end_date": start.date().isoformat(),
        "hourly": "temperature_2m,weather_code,wind_speed_10m,wind_direction_10m",
        "wind_speed_unit": "kmh",
        "timezone": "GMT",
    }
    response = http(url, params=params, timeout=5)
    response.raise_for_status()
    if len(response.content) > 64_000:
        raise ValueError("historical weather response too large")
    hourly = response.json().get("hourly") or {}
    times = hourly.get("time") or []
    nearest = min(
        range(len(times)),
        key=lambda i: abs(datetime.fromisoformat(times[i]).replace(tzinfo=UTC) - start),
        default=None,
    )
    if nearest is None:
        return None
    observed = datetime.fromisoformat(times[nearest]).replace(tzinfo=UTC)
    if abs(observed - start) > timedelta(minutes=30):
        return None

    def at(key):
        values = hourly.get(key) or []
        return values[nearest] if nearest < len(values) else None

    code = at("weather_code")
    line = publication_format.weather_line(
        publication_format.WMO.get(code) if type(code) is int else None,
        at("temperature_2m"),
        at("wind_speed_10m"),
        None,
        at("wind_direction_10m"),
        language,
        **units,
    )
    if line is None:
        return None
    return line, {
        "source": url,
        "time": observed.isoformat(),
        "latitude": params["latitude"],
        "longitude": params["longitude"],
        "method": "hourly historical reanalysis",
    }


def safety_level(rt, day: date) -> str:
    """Strictest fixed safety level for the activity day and today; fails closed."""
    from vibesynergy.core.timeutil import today_in
    from vibesynergy.query import daily_coach

    try:
        levels = set()
        for target in {day, today_in(rt.settings.timezone)}:
            with rt.engine.connect() as conn:
                conn.exec_driver_sql("BEGIN")
                captured = daily_coach._capture(rt, conn, target, "evening")
            levels.add(captured["inputs"]["safety"]["level"])
    except Exception:
        return "refuse"
    return "refuse" if "refuse" in levels else "flag" if "flag" in levels else "ok"


def collect(
    rt, activity_id: str, *, language: str = "en", weather=None, insight=None, safety=None
) -> tuple[dict[str, str], dict]:
    """Enabled sections by plugin name (weather, quote) and their states.

    ``insight`` and ``safety`` are accepted and ignored. Publishing stays statistics,
    weather and quote.
    """
    del insight, safety
    identity = views.resolve_activity(rt.engine, activity_id)
    settings = load_settings(rt.settings.config_path).publication
    sections: dict[str, str] = {}
    states = {}
    for name in PLUGINS:
        if not settings.get(f"{name}_enabled", True):
            states[name] = {"status": "disabled", "version": VERSION}
            continue
        if name == "weather" and indoor(rt, activity_id):
            # Checked before the cache, so an earlier weather line is never reused here.
            states[name] = {"status": "not_applicable", "reason": "indoor", "version": VERSION}
            continue
        # A different unit choice makes a new weather line, not the cached one.
        extra_key = (
            settings.get("quote_themes")
            if name == "quote"
            else (rt.settings.temperature, rt.settings.wind)
        )
        old = _cached(rt, identity, name)
        cache_version = hashlib.sha256(
            str((VERSION, name, identity, language, extra_key)).encode()
        ).hexdigest()
        if old and old["version"] == cache_version:
            sections[name] = old["section"]
            states[name] = {
                "status": "ok",
                "version": VERSION,
                "provenance": old["provenance"],
                "cached": True,
            }
            continue
        try:
            if name == "quote":
                result = _quote(
                    rt,
                    identity,
                    language,
                    settings.get("quote_themes", list(THEMES)),
                    settings.get("quote_sources", list(COLLECTIONS)),
                    settings.get("quote_providers", list(PROVIDERS)),
                )
            else:
                result = (weather or _weather)(rt, activity_id, language)
            if result is None:
                states[name] = {"status": "unavailable", "version": VERSION}
            else:
                section, provenance = result
                if len(section) > 500 or len(str(provenance)) > 1000:
                    raise ValueError("plugin result too large")
                provenance = {**provenance, "language": language}
                _save(rt, identity, name, section, provenance, cache_version)
                states[name] = {"status": "ok", "version": VERSION, "provenance": provenance}
                sections[name] = section
                continue
        except Exception as exc:
            if not isinstance(
                exc, requests.RequestException | ValueError | KeyError | IndexError | TypeError
            ):
                raise
            states[name] = {"status": "failed", "version": VERSION}
        if (
            old
            and states[name]["status"] in {"failed", "unavailable"}
            and old["provenance"].get("language") == language
        ):
            sections[name] = old["section"]
            states[name]["reused"] = True
            states[name]["provenance"] = old["provenance"]
    return sections, states


def _local_date(rt, activity_id: str) -> date:
    group = views.resolve_activity(rt.engine, activity_id)
    with rt.engine.connect() as conn:
        return conn.scalar(
            select(activity_unified.c.local_date).where(activity_unified.c.group_id == group)
        )
