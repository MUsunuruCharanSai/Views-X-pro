"""Analyzer module using Google Gemini API and YouTube Transcript with refined viral scoring prompts, multi-model fallback, and strict JSON extraction."""

import json
import re
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from youtube_transcript_api import YouTubeTranscriptApi

from config import (
    DEFAULT_GEMINI_MODEL,
    FALLBACK_GEMINI_MODELS,
    GEMINI_API_KEY,
    MAX_CLIP_DURATION_SEC,
    MIN_CLIP_DURATION_SEC,
    MIN_CLIP_SPACING_SEC,
    MIN_CONFIDENCE_THRESHOLD,
    SCORING_WEIGHTS,
    TARGET_CLIP_COUNT,
    TEMP_DIR,
)
from utils import logger

_TRANSCRIPT_CACHE: Dict[str, List[Dict[str, Any]]] = {}


def clear_transcript_cache(video_id: Optional[str] = None):
    """Clear transcript cache to prevent memory accumulation across jobs."""
    if video_id:
        _TRANSCRIPT_CACHE.pop(video_id, None)
        logger.debug(f"Cleared transcript cache for video {video_id}")
    else:
        _TRANSCRIPT_CACHE.clear()
        logger.debug("Cleared entire transcript cache")


# Reaction / peak-emotion vocabulary common in gaming, streaming, IRL content
STREAMING_REACTION_WORDS = {
    # shock / excitement / screams
    "yoo", "yooo", "yoooo", "yooooo", "noooo", "nooo", "noo", "omg", "holy",
    "insane", "crazy", "unreal", "incredible", "unbelievable", "what", "wait",
    "no way", "no", "bro", "bruh", "dude", "oh my god", "what the", "ain't no way",
    # gaming specific
    "clip it", "clip", "clutch", "pog", "poggers", "letsgo", "lets go",
    "gg", "ggs", "rip", "dead", "die", "dies", "killed", "rage",
    "won", "win", "lost", "loss", "game over", "one shot", "oneshot",
    "chat", "chat went", "chat is", "twitch", "kick", "stream",
    # reactions / drama
    "lmao", "lol", "xd", "haha", "hahaha", "laugh", "crying",
    "stop", "done", "finished", "over", "out", "exposed", "shut up",
    "liar", "cheater", "banned", "caught", "screaming", "meltdown",
}

# Curiosity / hook question trigger words
CURIOSITY_HOOK_TRIGGERS = {
    "why", "how", "what if", "did you know", "you won't believe",
    "nobody knows", "secret", "never", "the truth about", "i tried",
    "watch this", "listen to this", "look at this", "this changed",
    "worst mistake", "biggest mistake", "the real reason",
}


def _build_hook_from_text(text: str, max_len: int = 60) -> str:
    """Build a distinct, ALL-CAPS hook line from a real transcript snippet."""
    cleaned = re.sub(r"\s+", " ", (text or "")).strip()

    # Strip leading auto-caption speaker-change markers
    cleaned = re.sub(r"^[>»\-–—\s]+", "", cleaned)

    # Strip common filler/stutter words that make poor standalone hooks
    cleaned = re.sub(r"\b(um|uh|like|you know|i mean|basically)\b", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    # Strip leading/trailing fragments that are ONLY punctuation
    cleaned = re.sub(r"^[\.,;:!?\s]+", "", cleaned)
    cleaned = re.sub(r"[\.,;:\s]+$", "", cleaned)
    cleaned = cleaned.strip()

    if not cleaned or len(cleaned) < 4 or not re.search(r"[A-Za-z]", cleaned):
        return "YOU WON'T BELIEVE THIS"

    hook = cleaned.upper()
    if len(hook) > max_len:
        hook = hook[: max_len - 3].rstrip() + "..."
    return hook


def extract_youtube_id(url: str) -> Optional[str]:
    """Extract 11-character YouTube video ID from various URL formats."""
    pattern = r"(?:v=|shorts/|youtu\.be/|embed/|/v/)([0-9A-Za-z_-]{11})"
    match = re.search(pattern, url)
    return match.group(1) if match else None


def compute_derived_score(clip: Dict[str, Any]) -> float:
    """Compute overall confidence score as a weighted sum of the 4 sub-factor scores."""
    # Guard against LLM returning JSON null for numeric fields — float(None) would crash
    hook = float(clip.get("hook_strength") or 0.7)
    emotion = float(clip.get("emotional_intensity") or 0.7)
    unexp = float(clip.get("unexpectedness") or 0.7)
    payoff = float(clip.get("payoff_quality") or 0.7)

    w_hook = SCORING_WEIGHTS["hook_strength"]
    w_emo = SCORING_WEIGHTS["emotional_intensity"]
    w_unexp = SCORING_WEIGHTS["unexpectedness"]
    w_payoff = SCORING_WEIGHTS["payoff_quality"]

    score = (w_hook * hook) + (w_emo * emotion) + (w_unexp * unexp) + (w_payoff * payoff)
    return max(0.0, min(1.0, round(score, 3)))


def parse_time_to_seconds(val: Any) -> float:
    """Parse float seconds or HH:MM:SS / MM:SS timestamp string into float seconds."""
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        val = val.strip()
        parts = val.split(":")
        try:
            if len(parts) == 3:
                return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
            elif len(parts) == 2:
                return float(parts[0]) * 60 + float(parts[1])
            return float(val)
        except ValueError:
            return 0.0
    return 0.0


def _normalize_snippet(item: Any) -> Dict[str, Any]:
    """Normalize raw transcript item into Dict[start, duration, text]."""
    if isinstance(item, dict):
        return {
            "start": float(item.get("start", 0.0)),
            "duration": float(item.get("duration", 2.0)),
            "text": str(item.get("text", "")).strip()
        }
    return {
        "start": float(getattr(item, "start", 0.0)),
        "duration": float(getattr(item, "duration", 2.0)),
        "text": str(getattr(item, "text", "")).strip()
    }


# ── Known Creator Database ────────────────────────────────────────────────────
# Maps channel name substrings (lowercase) to creator-specific context.
# Used by _research_creator_context() to inject creator intelligence into the LLM prompt.
# Key = substring to match against oEmbed author_name.lower()
KNOWN_CREATOR_DB = {
    "ishowspeed": {
        "display_name": "IShowSpeed",
        "content_niche": "irl_stream",
        "primary_games": "International IRL Tours, Stunts, Athletics, Variety Gaming",
        "current_career_era": "Global Superstar & International IRL Era (2024–2026). No longer just a room streamer; travels countries (Southeast Asia, Europe, Brazil), gets swarmed by 50,000+ fans in the streets, meets world leaders, and does superhuman athletic stunts.",
        "known_for": "jumping over speeding Lamborghinis/supercars live, racing Olympic sprinters (Ewa Swoboda, Noah Lyles), massive street mobs in foreign countries requiring police escorts, WWE WrestleMania/Royal Rumble appearances, cultural immersion turned chaotic (crashing TukTuks, Muay Thai), legendary explosive screaming and backflips",
        "catchphrases": ["SPEED IS EVERYWHERE", "WHAT THE SIGMA", "SHE'S BEAUTIFUL", "ROOON", "SIUUU", "LET'S GOOO", "WAHHHH", "SHEEEESH", "AURA", "NO WAY"],
        "viral_clip_patterns": [
            "Massive foreign crowd swarming Speed — police escort, chanting his name, pure uncontrollable street mania",
            "Superhuman athletic stunt: jumping over a speeding luxury supercar, sprinting against pro athletes, parkour backflips in public",
            "Speed tries a foreign cultural tradition or martial art (Muay Thai, driving elephants, street food) and it turns abruptly into comedy/chaos",
            "A-list celebrity or sports legend encounter (Manny Pacquiao, Ronaldo, Zlatan, WWE wrestlers)",
            "Classic 0-to-100 Speed crashout: calm moment that escalates into barking, screaming, or crying in 2 seconds",
        ],
        "subscribe_psychology": "Viewers subscribe because Speed has the highest individual aura and energy on the internet — every stream feels like a worldwide blockbuster movie where anything can happen. First-time viewers freeze mid-scroll at the scale of crowds or the absurdity of his stunts and hit Subscribe to not miss his next country.",
        "hook_style": "Hook immediately on the spectacle: either the massive crowd chant, the car speeding directly at him, or his face right as the situation goes out of control.",
    },
    "kai cenat": {
        "display_name": "Kai Cenat",
        "content_niche": "gaming_stream_react",
        "primary_games": "Theatrical 100hr Marathons (Elden Ring, Bloodborne), A-List Celebrity Collabs, IRL",
        "current_career_era": "King of Twitch & Hollywood Cultural Tastemaker (2024–2026). Two-time Streamer of the Year, Mafiathon all-time sub record holder (400k+ subs), bridging streaming with mainstream film, music, and gaming industry titans.",
        "known_for": "100+ hour grueling FromSoftware marathons (Elden Ring, Shadow of the Erdtree, Bloodborne) in full theatrical sets with therapists on standby, legendary comedy duo streams with Kevin Hart and Druski, A-list guest streams (John Cena, Snoop Dogg, Lil Wayne, Tyla), 7-day prison streams, Ray (Taiwanese friend) comedy dynamic",
        "catchphrases": ["YO CHAT", "IT'S GIVING", "W OR L", "NAHHH BRO", "MAFIA", "LOCK IN", "AURA", "RIZZ", "NO CAP", "THIS IS CRAZY", "RAYYY"],
        "viral_clip_patterns": [
            "Kevin Hart or Druski roasting Kai — peak buddy-comedy banter where Kai gets completely outmatched or claps back",
            "Climax of a 60+ hour Elden Ring boss fight (Malenia, Radahn) where Kai screams, cries, or collapses in genuine exhaustion and triumph",
            "Ray (his Taiwanese friend) saying something wildly out of pocket or hilarious cultural misunderstandings",
            "High-budget Hollywood-level stream bit (building an entire functioning prison, courtroom, or medieval castle inside his stream room)",
            "A-list celebrity guest gets caught off guard by Twitch culture, chat memes, or sudden chaos in the room",
        ],
        "subscribe_psychology": "Viewers subscribe because Kai Cenat is the epicenter of internet culture — tuning into Kai feels like being invited to the most exclusive, entertaining VIP party in the world. New viewers subscribe for the contagious camaraderie and FOMO of missing the next legendary marathon or celebrity moment.",
        "hook_style": "Start on the celebrity punchline or the high-tension boss fight countdown. Kai's facial reaction or the guest's bewilderment stops the feed immediately.",
    },
    "jasontheween": {
        "display_name": "JasonTheWeen",
        "content_niche": "gaming_stream_react",
        "primary_games": "FaZe House Streams, Viral Music, E-Dating Arcs, International IRL (Asia/NYC)",
        "current_career_era": "FaZe Clan Star & Viral Music/Lifestyle Icon (2024–2026). Joined the rebooted FaZe Clan, moved into the FaZe House, transitioned from Discord e-dating into releasing hit music, high-fashion IRL streams, and multi-episode romantic storylines.",
        "known_for": "FaZe house room antics, the other FaZe boys (Lacy, Ronaldo, Banks) breaking into his room to roast him on dates, viral R&B/pop tracks, romantic storylines with girls from around the world, piano serenades, awkward cringe humor turned charming",
        "catchphrases": ["WE'RE SO BACK", "IT'S OVER", "W RIZZ", "SHE'S 10/10", "CHAT BE FOR REAL", "OOGA BOOGA", "ON GOD", "HELL NAH", "FAZE UP", "MAX AURA"],
        "viral_clip_patterns": [
            "Jason is on a call or in-person date with a girl and says something so painfully awkward or unhinged she freezes",
            "Lacy or Ronaldo barges into Jason's room during a serious or romantic moment and causes absolute mayhem",
            "Jason plays piano and sings a surprisingly good song or hilarious freestyle that shocks chat and the guest",
            "Jason pulls off an unexpected 'W RIZZ' moment and chat explodes with 'WE'RE SO BACK'",
            "Devastated facial reaction when a girl rejects him, roasts his height, or friendzones him on stream",
        ],
        "subscribe_psychology": "Viewers subscribe for the ongoing romantic sitcom lore — Jason is relatable, self-deprecating, and unintentionally hilarious. Every short feels like a 30-second episode of a reality show where you want to know if he wins or loses in the end.",
        "hook_style": "Start on the uncomfortable question or the girl's stunned expression right before Jason replies. The cringe/romantic tension is magnetic.",
    },
    "adapt": {
        "display_name": "FaZe Adapt",
        "content_niche": "gaming_fps",
        "primary_games": "Call of Duty / Warzone, FaZe House IRL, Boxing, High-Stakes Lifestyle",
        "current_career_era": "OG FaZe Clan Patriarch & Veteran Anchor (2024–2026). The defining bridge between classic Call of Duty FaZe and the explosive 2024 reboot; acts as the respected veteran brother in the FaZe house managing the new generation.",
        "known_for": "high-level competitive Call of Duty / Warzone clutches, veteran FaZe house meetings with FaZe Banks, mentoring Lacy/Ronaldo/Jason, boxing and fitness transformations, luxury lifestyle vlogs, nostalgic storytelling of old gaming eras",
        "catchphrases": ["THAT'S COLD", "WE MOVE", "LET'S GO", "FAZE UP", "BRO ON GOD", "OG FAZE", "LOCK IN"],
        "viral_clip_patterns": [
            "Insane skillshot, sniper feed, or 1v4 clutch in Warzone/CoD that proves his veteran pedigree",
            "FaZe house meeting where Adapt has to break up a crazy fight between Lacy and Ronaldo or deliver real talk",
            "Adapt reacting to how unhinged the younger FaZe generation is with a hilarious 'I've seen it all' expression",
            "Emotional or nostalgic reflections on classic gaming that strike a chord with older and new fans alike",
        ],
        "subscribe_psychology": "Viewers subscribe for authenticity, legacy, and elite gaming skill — Adapt is the cool, grounded older brother of internet culture who has survived every era and still dominates.",
        "hook_style": "Start on the sniper crosshair alignment or the dramatic opening line of a house interaction.",
    },
    "stable ronaldo": {
        "display_name": "Stable Ronaldo",
        "content_niche": "gaming_stream_react",
        "primary_games": "FaZe House Chaos, Fortnite Competitive, High-Stakes IRL, Police/Public Encounters",
        "current_career_era": "FaZe House Lightning Rod & Viral Crashout Machine (2024–2026). One of the core stars of the FaZe reboot, known for chaotic public IRL moments, explosive domestic arguments in the house, and hyperactive comedy.",
        "known_for": "high-strung screaming matches with FaZe Lacy, getting stopped by police or swatting incidents during IRL streams, high-energy Fortnite competitive rants, pulling wild pranks in the FaZe house, relationship drama with Angell",
        "catchphrases": ["VAMOS", "GOLAZO", "I'M DEAD", "BRO ACTUALLY", "ON MY LIFE", "CHAT LOOK AT THIS", "I'M NOT CRAZY", "BRO RELAX"],
        "viral_clip_patterns": [
            "Ronaldo gets triggered by chat or Lacy and goes into a rapid-fire, high-pitch rant with unmatched vocal intensity",
            "FaZe house confrontation where Ronaldo and Lacy are chest-to-chest screaming over something completely petty",
            "Public IRL encounter: security, police, or confused bystanders reacting to Ronaldo's manic energy",
            "Ronaldo reacting to internet drama or TikTok clips of himself with utter disbelief",
        ],
        "subscribe_psychology": "Viewers subscribe for Ronaldo's raw, uninhibited emotional fireworks — he has zero filter and reacts with 1000% intensity to everything. Viewers love the sheer comedic velocity of his reactions.",
        "hook_style": "Start on the sudden audio spike or right as his voice cracks into a scream. The instant burst of energy stops anyone from swiping.",
    },
    "lacy": {
        "display_name": "FaZe Lacy",
        "content_niche": "gaming_stream_react",
        "primary_games": "FaZe House IRL, Physical Crashouts, Fortnite, Fitness Transformation",
        "current_career_era": "The Internet's Crashout King & Breakout FaZe Entertainer (2024–2026). Joined FaZe Clan in May 2024, shaved head and eyebrows live on stream, underwent an inspiring gym transformation while remaining the master of physical comedy.",
        "known_for": "0-to-100 unhinged crashouts, ripping his shirt off, breaking monitors and chairs, screaming until his face turns red, chaotic arguments with Stable Ronaldo, hilarious physical stunts in the FaZe mansion",
        "catchphrases": ["I'M CRASHING OUT", "LOCK IN", "ON MY MAMA", "CHAT NO WAY", "BROOO", "WHY ARE YOU WEIRD", "I CAN'T TAKE THIS", "CHILL CHILL CHILL"],
        "viral_clip_patterns": [
            "Lacy fully crashing out: screaming at maximum decibels, ripping off his shirt, kicking over gaming equipment",
            "Lacy and Ronaldo screaming insults at each other in the FaZe kitchen over food or house chores",
            "Lacy attempts a physical stunt, gymnastics, or gym lift that fails hilariously",
            "Lacy getting roasted by FaZe Banks or guests and having an exaggerated comedic breakdown",
        ],
        "subscribe_psychology": "Viewers subscribe because Lacy's crashouts are peak slapstick comedy — he embraces being the lovable chaos agent who puts his entire body and soul into entertaining the stream.",
        "hook_style": "Start right before the voice crack or before he jumps out of his chair. The visual and audio explosion is impossible to ignore.",
    },
    "ludwig": {
        "display_name": "Ludwig",
        "content_niche": "gaming_stream_react",
        "primary_games": "Mega-Event Production, Mogul Mail / Streaming Journalism, The Yard, Game Shows",
        "current_career_era": "Media Mogul & Premier Live Event Producer (2024–2026). Founder of Offbrand and Mogul Moves, co-owner of Moist Esports & Shopify Rebellion; creator of stadium-scale live streamer sports tournaments and high-production game shows.",
        "known_for": "The Streamer Games (stadium-size Olympic games for streamers), Mogul Chessboxing Championships, Unpaid Intern (live interactive corporate game show), sharp video essays on copyright lawsuits and streaming platform wars, The Yard comedy podcast",
        "catchphrases": ["BOYS TODAY THE PLAN IS SIMPLE", "THAT'S CRAZY", "WHAT A WORLD", "MOGUL MOVES", "NO SHOT", "POGCHAMP"],
        "viral_clip_patterns": [
            "A high-stakes or funny athletic moment from The Streamer Games or Chessboxing with top creators competing",
            "Ludwig drops an incisive, well-researched truth bomb about YouTube/Twitch drama or copyright strikes",
            "Unpaid Intern contestant makes a shockingly unhinged choice that breaks Ludwig's professional composure",
            "The Yard podcast comedic riff where Ludwig gets roasted by his co-hosts or delivers a hilarious story",
        ],
        "subscribe_psychology": "Viewers subscribe because Ludwig represents high production value and intellectual humor in the creator space — subscribing means you get access to the most creative big-budget events and smartest commentary on the internet.",
        "hook_style": "Lead with the provocative question, the absurd athletic premise, or the insider reveal that challenges what viewers thought they knew.",
    },
    "jynxzi": {
        "display_name": "Jynxzi",
        "content_niche": "gaming_fps",
        "primary_games": "The Jynxzi Show Podcast, High-Stakes R6 1v1 Tournaments, IRL Dating Shows",
        "current_career_era": "Top Tier Broadcaster & Interviewer (2024–2026). Transitioned from purely Rainbow Six Siege into hosting 'The Jynxzi Show' (the biggest gaming podcast on Twitch), massive IRL reality dating competitions, and stadium sporting crossovers.",
        "known_for": "The Jynxzi Show interviewing top internet royalty (CaseOh, Bradley Martyn, Sketch, Kai Cenat), '20 Women vs 1 Jynxzi' dating shows, his iconic duo with Sketch ('What's up brother'), $10K-$50K high-stakes 1v1 Rainbow Six tournaments, dramatic rage that flips into comedy",
        "catchphrases": ["WHAT IS THAT", "BRO WHAT", "ARE YOU KIDDING ME", "ACTUAL HACKER", "I CANNOT", "GOOD AIM", "BAP BAP", "SPECIAL TEAMS SPECIAL PLAYS", "WHAT'S UP BROTHER"],
        "viral_clip_patterns": [
            "Podcast moment on The Jynxzi Show: a guest reveals something unbelievable or Jynxzi asks an outrageously blunt question",
            "Jynxzi and Sketch in person doing their signature banter, NFL stadium antics, or athletic face-offs",
            "IRL Dating show moment: Jynxzi gets rejected, caught in a lie detector test, or delivers an awkward romantic line",
            "1v5 or final round overtime clutch in Rainbow Six with insane mechanical aim followed by chair-slamming celebration",
        ],
        "subscribe_psychology": "Viewers subscribe because Jynxzi blends competitive elite skill with unhinged entertainment — whether he is grilling a celebrity guest, failing a blind date, or winning a $50K clutch, he is 100% authentic.",
        "hook_style": "Start on the guest's shocking statement or the final 1v1 countdown. The anticipation hooks viewers instantly.",
    },
    "sketch": {
        "display_name": "Sketch",
        "content_niche": "gaming_stream_react",
        "primary_games": "Madden, NFL Collabs, IRL, Variety Gaming",
        "current_career_era": "Mainstream Comedy & NFL Crossover Phenom (2024–2026). Exploded into global pop culture with signature mannerisms, collaborated with NFL superstars (C.J. Stroud, Will Levis) and became a staple across Kai Cenat and Jynxzi's streams.",
        "known_for": "iconic hand gestures, 'What's up brother', 'Special teams special plays special players', NFL draft appearances, playing Madden with real NFL players, wholesome deadpan humor",
        "catchphrases": ["WHAT'S UP BROTHER", "SPECIAL TEAMS SPECIAL PLAYS", "TUESDAY TUESDAY", "SORRY ABOUT THAT", "THANK YOU VERY MUCH"],
        "viral_clip_patterns": [
            "Sketch hits his iconic finger point and 'What's up brother' greeting with a bewildered celebrity or athlete",
            "NFL star joins Sketch on stream and tries to understand his mannerisms and gaming slang",
            "Sketch pulls off an impossible Madden play and celebrates with deadpan enthusiasm",
        ],
        "subscribe_psychology": "Viewers subscribe for wholesome, quoteable comedy — Sketch is universally loved across sports, gaming, and mainstream culture.",
        "hook_style": "Start on the iconic gesture or the athlete's reaction to his phrase.",
    },
    "fanum": {
        "display_name": "Fanum",
        "content_niche": "irl_stream",
        "primary_games": "NYC / Miami IRL, AMP House Content, Supercar Drives, Food Reviews",
        "current_career_era": "AMP Patriarch & Global Slang Originator (2024–2026). Originator of the worldwide 'Fanum Tax' phenomenon, leader of the AMP collective, host of high-octane NYC street driving streams and Dominican cultural celebrations.",
        "known_for": "The Fanum Tax (stealing food from Kai and AMP members), high-speed NYC car streams, driving customized purple supercars, Dominican Independence Day streams, AMP house family dynamic",
        "catchphrases": ["FANUM TAX", "WE OUTSIDE", "DOMINICANO", "WOCKY", "YERRR", "CHAT WE LIT"],
        "viral_clip_patterns": [
            "Fanum sneaks up on Kai Cenat or another streamer and executes a textbook 'Fanum Tax' food heist",
            "High-energy NYC street encounter or exotic car cruise through Manhattan with fans chasing him",
            "AMP house group argument over food, video games, or pranks where Fanum acts as the comedic enforcer",
        ],
        "subscribe_psychology": "Viewers subscribe for raw NYC energy, infectious laughter, and real community brotherhood — Fanum makes every viewer feel like part of the crew.",
        "hook_style": "Start on the food snatch or the car engine roar. High energy right from frame one.",
    },
}

# Match aliases — alternate names / handle variations that map to the same creator
_CREATOR_ALIASES = {
    "speed": "ishowspeed",
    "i show speed": "ishowspeed",
    "kaicenat": "kai cenat",
    "kai": "kai cenat",
    "jason": "jasontheween",
    "jasonthween": "jasontheween",
    "jasontheweenie": "jasontheween",
    "jason the ween": "jasontheween",
    "faze jason": "jasontheween",
    "adaptlive": "adapt",
    "adapt live": "adapt",
    "faze adapt": "adapt",
    "ronaldo": "stable ronaldo",
    "stableronaldo": "stable ronaldo",
    "faze ronaldo": "stable ronaldo",
    "faze stable ronaldo": "stable ronaldo",
    "lacy": "lacy",
    "fazelacy": "lacy",
    "faze lacy": "lacy",
    "jynx": "jynxzi",
    "jynxzi live": "jynxzi",
    "lud": "ludwig",
    "ludwigahgren": "ludwig",
    "ludwig ahgren": "ludwig",
    "sketch": "sketch",
    "thesketchreal": "sketch",
    "the sketch": "sketch",
    "fanum": "fanum",
    "fanum live": "fanum",
    "justfanum": "fanum",
}

# ── Content category detection keyword sets ───────────────────────────────────
_CONTENT_CATEGORIES = {
    "gaming": {
        # Platform / stream meta
        "stream", "streaming", "streamer", "twitch", "kick", "youtube",
        "chat", "subscribers", "raid", "follow", "donate", "bits",
        "pog", "poggers", "pogchamp", "gg", "ggs",
        # General gameplay
        "clip", "clutch", "kill", "kills", "killed", "headshot", "snipe",
        "game", "gaming", "gameplay", "round", "level", "map", "lobby",
        "win", "won", "lose", "lost", "died", "respawn", "revive", "queue",
        "fps", "hp", "health", "ammo", "loot", "squad", "team",
        "rage", "toxic", "ranked", "rank",
        # FPS specific (Valorant, CoD, R6S, Apex, CS)
        "operator", "ace", "plant", "defuse", "bomb", "site", "flank",
        "valorant", "warzone", "call of duty", "rainbow six", "siege", "r6",
        "apex legends", "counter strike", "fortnite",
        "sweat", "one tap", "sprayed", "no scope", "wall bang",
        # FIFA / sports games
        "fifa", "pack", "ultimate team", "fut", "goalkeeper",
        "goal", "assist", "transfer", "squad builder",
        # Minecraft / sandbox
        "minecraft", "creeper", "craft", "build", "blocks", "endgame",
        "nether", "diamond", "speedrun",
    },
    "gaming_fps": {
        "rainbow six", "siege", "r6", "valorant", "warzone", "call of duty",
        "apex", "fortnite", "counter strike", "cs2",
        "headshot", "ace", "clutch", "one tap", "snipe", "flank", "operator",
        "plant", "defuse", "bomb", "spray", "wall bang", "jynxzi",
    },
    "gaming_sports": {
        "fifa", "ea fc", "ultimate team", "fut", "pack opening",
        "goal", "goalkeeper", "assist", "transfer", "player rating",
        "bundesliga", "premier league", "la liga", "champions league",
        "stable ronaldo", "vamos", "golazo",
    },
    "gaming_stream_react": {
        "ishowspeed", "kai cenat", "ludwig", "speed", "kai", "jynxzi",
        "chat went crazy", "chat is going insane", "sub-athon", "subathon",
        "donation", "charity", "raid", "host", "gifted subs",
        "mafia", "rizz", "sigma", "it's giving",
    },
    "podcast": {
        "podcast", "episode", "guest", "host", "welcome back to the show",
        "sponsor", "patreon", "listener", "interview", "conversation",
        "in your career", "tell me about", "fascinating", "perspective",
    },
    "drama": {
        "drama", "exposed", "apology", "apologized", "canceled", "beef",
        "allegations", "receipts", "leaked", "scandal", "truth about",
        "calling out", "response to", "statement", "lawsuit",
    },
    "vlog": {
        "vlog", "today we are", "welcome to my", "traveling to",
        "flight", "hotel", "packing", "room tour", "buying", "shopping",
        "eating at", "trying out", "day in the life",
    },
    "irl_stream": {
        "irl", "in real life", "stream sniper", "stream sniping", "public",
        "stranger", "restaurant", "store", "police", "security", "mall",
        "uber", "hotel", "airport", "crowd", "camera man", "backpack stream",
        "live in public", "faze house", "japan", "korea", "nyc", "miami",
        "street", "talking to girls", "walking around", "asking strangers",
        "bro look at this guy", "he doesn't know we're live",
    }
}


def _detect_content_type(transcript: List[Dict[str, Any]]) -> str:
    """Auto-detect whether transcript is gaming, podcast, drama, vlog, or general."""
    if not transcript:
        return "general"

    all_text = " ".join(item.get("text", "") for item in transcript[:600]).lower()
    
    category_scores = {}
    for cat, kws in _CONTENT_CATEGORIES.items():
        count = sum(1 for kw in kws if kw in all_text)
        category_scores[cat] = count

    best_cat, best_score = max(category_scores.items(), key=lambda x: x[1])
    content_type = best_cat if best_score >= 3 else "general"
    
    logger.info(
        f"[Content Detection] Category matches: {category_scores} -> content_type='{content_type}'"
    )
    return content_type



def parse_vtt_subtitle(vtt_path: Path) -> List[Dict[str, Any]]:
    """Parse local WebVTT subtitle file generated by yt-dlp into transcript format."""
    if not vtt_path.exists():
        return []

    snippets = []
    try:
        content = vtt_path.read_text(encoding="utf-8", errors="ignore")
        blocks = re.split(r"\n\s*\n", content)

        for block in blocks:
            lines = [l.strip() for l in block.splitlines() if l.strip()]
            if not lines:
                continue

            time_line = None
            text_lines = []
            for line in lines:
                if "-->" in line:
                    time_line = line
                elif not line.isdigit() and not line.startswith("WEBVTT") and not line.startswith("NOTE"):
                    text_lines.append(line)

            if time_line and text_lines:
                times = time_line.split("-->")
                if len(times) == 2:
                    def parse_time(t_str):
                        t_str = t_str.strip().replace(",", ".")
                        parts = t_str.split(":")
                        if len(parts) == 3:
                            return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
                        elif len(parts) == 2:
                            return float(parts[0]) * 60 + float(parts[1])
                        return 0.0

                    start_sec = parse_time(times[0])
                    end_sec = parse_time(times[1])
                    dur = max(0.5, end_sec - start_sec)
                    clean_text = re.sub(r"<[^>]+>", "", " ".join(text_lines)).strip()
                    if clean_text:
                        snippets.append({"start": round(start_sec, 2), "duration": round(dur, 2), "text": clean_text})

        logger.info(f"Successfully parsed {len(snippets)} transcript snippets from local VTT subtitle file: {vtt_path.name}")
        return snippets
    except Exception as e:
        logger.warning(f"Could not parse VTT subtitle file ({e}): {vtt_path}")
        return []


def _extract_json_substring(text: str) -> Optional[str]:
    """Find and return the first structurally balanced JSON array or object substring.
    
    Bug 4 fix: Strips <think>...</think> preambles and extracts structurally balanced
    JSON substrings without falling victim to prose regex failures.
    """
    if not text or not isinstance(text, str):
        return None
    clean_text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE).strip()

    first_open = None
    open_char = None
    close_char = None
    stack = 0
    in_string = False
    escape = False

    for i, ch in enumerate(clean_text):
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
            continue

        if first_open is None:
            if ch in ("[", "{"):
                first_open = i
                open_char = ch
                close_char = "]" if ch == "[" else "}"
                stack = 1
        else:
            if ch == open_char:
                stack += 1
            elif ch == close_char:
                stack -= 1
                if stack == 0:
                    return clean_text[first_open : i + 1]
    return None


# ── Parasocial / Audience relationship signals ────────────────────────────────
AUDIENCE_CALLBACK_PHRASES = {
    "you guys", "you asked", "from the comments", "you wanted", "you said",
    "you told me", "chat wanted", "chat said", "the community", "my viewers",
    "you guys told me", "based on your feedback", "you all asked",
}

CREATOR_AUTHENTICITY_MARKERS = {
    "not gonna lie", "ngl", "honestly", "i actually", "real talk",
    "i have to be honest", "to be real", "no cap", "lowkey", "genuinely",
    "i'm not gonna lie", "if i'm being honest", "this is embarrassing",
    "i never told anyone", "between us", "i hate to admit",
}

SERIES_CONTINUITY_SIGNALS = {
    "last time", "last episode", "last week", "remember when",
    "as i mentioned", "like i said before", "you know how i always",
    "since we started", "day one", "our tradition", "we always do this",
    "part of the journey", "still going", "back at it",
}

CREATOR_SELFID_PHRASES = {
    "welcome back", "if you're new here", "as you guys know",
    "for those who don't know", "i always say", "this is what i do",
    "this is how we do it", "on this channel", "what we do here",
    "subscribe if you", "hit that subscribe", "follow for more",
}

# ── Content series / format detection patterns ────────────────────────────────
_SERIES_PATTERNS = [
    # Episode / numbered formats
    (r"\bep\.?\s*\d+\b", "episode"),
    (r"\bepisode\s+\d+\b", "episode"),
    (r"#\d{2,}", "episode"),
    (r"\bpart\s+\d+\b", "series_part"),
    (r"\bday\s+\d+\b", "daily_series"),
    (r"\bweek\s+\d+\b", "weekly_series"),
    (r"\bseason\s+\d+\b", "season"),
    # Challenge/experiment formats
    (r"\bi tried\b", "challenge"),
    (r"\b24\s*hours?\b", "challenge"),
    (r"\b\d+\s*days?\b", "challenge"),
    (r"\bvs\.?\b", "versus"),
    (r"\bchallenge\b", "challenge"),
    (r"\bexperiment\b", "experiment"),
]


class VideoAnalyzer:
    """Analyzes transcript and identifies viral hook moments using LLMs with refined capability prompts."""

    def __init__(self, api_key: Optional[str] = None, provider: Optional[str] = None, settings: Optional[Dict[str, Any]] = None):
        settings = settings or {}
        self.provider = "gemini"

        passed_key = (api_key or "").strip()
        saved_gemini = (settings.get("gemini_api_key") or GEMINI_API_KEY or "").strip()
        self.gemini_key = passed_key or saved_gemini
        self.model = settings.get("gemini_model") or DEFAULT_GEMINI_MODEL

        self.target_clip_count = settings.get("target_clip_count", TARGET_CLIP_COUNT)
        self.min_confidence_threshold = settings.get("min_confidence_score", MIN_CONFIDENCE_THRESHOLD)
        self.min_clip_spacing_sec = settings.get("min_clip_spacing_seconds", MIN_CLIP_SPACING_SEC)
        self.min_clip_dur = settings.get("min_clip_duration_sec", MIN_CLIP_DURATION_SEC)
        self.max_clip_dur = settings.get("max_clip_duration_sec", MAX_CLIP_DURATION_SEC)

    def _research_creator_context(self, youtube_url: str, title: str, transcript: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Research the creator and video before selecting clips.

        Returns a context dict with:
          - author_name: channel name from oEmbed (no API key needed)
          - format_type: detected video format (episode/challenge/versus/standalone)
          - series_signal: human-readable series description or 'standalone'
          - top_catchphrases: list of up to 3 most-repeated multi-word phrases
          - selfid_count: how many creator self-identification signals found
          - audience_callback_count: how many audience-relationship signals found
          - authenticity_count: how many vulnerability/honesty markers found
          - audience_relationship: 'community-driven' / 'parasocial' / 'informational'
          - context_card: the formatted string to inject into the LLM prompt
        """
        ctx: Dict[str, Any] = {
            "author_name": "Unknown Channel",
            "author_url": "",
            "format_type": "standalone",
            "series_signal": "standalone video",
            "top_catchphrases": [],
            "selfid_count": 0,
            "audience_callback_count": 0,
            "authenticity_count": 0,
            "audience_relationship": "general",
            "context_card": "",
        }

        # ── 1. YouTube oEmbed fetch (no API key needed) ───────────────────────
        try:
            oembed_url = f"https://www.youtube.com/oembed?url={youtube_url}&format=json"
            req = urllib.request.Request(
                oembed_url,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AI-Clipper/9.0"}
            )
            with urllib.request.urlopen(req, timeout=6) as resp:
                oembed = json.loads(resp.read().decode("utf-8"))
            ctx["author_name"] = oembed.get("author_name", "Unknown Channel")
            ctx["author_url"] = oembed.get("author_url", "")
            logger.info(f"[Creator Research] oEmbed: channel='{ctx['author_name']}' title='{title}'")
        except Exception as e:
            logger.debug(f"[Creator Research] oEmbed fetch failed ({e}). Using fallback.")

        # ── 2. Title intelligence: series/format detection ────────────────────
        title_lower = (title or "").lower()
        detected_formats = []
        for pattern, fmt in _SERIES_PATTERNS:
            if re.search(pattern, title_lower, re.IGNORECASE):
                detected_formats.append(fmt)

        if "episode" in detected_formats:
            ctx["format_type"] = "episode"
            ctx["series_signal"] = "part of an ongoing episode series"
        elif "daily_series" in detected_formats:
            ctx["format_type"] = "daily_series"
            ctx["series_signal"] = "daily series / streak-based content"
        elif "weekly_series" in detected_formats:
            ctx["format_type"] = "weekly_series"
            ctx["series_signal"] = "weekly series"
        elif "season" in detected_formats:
            ctx["format_type"] = "season"
            ctx["series_signal"] = "part of a season series"
        elif "series_part" in detected_formats:
            ctx["format_type"] = "series_part"
            ctx["series_signal"] = "multi-part series"
        elif "challenge" in detected_formats:
            ctx["format_type"] = "challenge"
            ctx["series_signal"] = "challenge / experiment format"
        elif "versus" in detected_formats:
            ctx["format_type"] = "versus"
            ctx["series_signal"] = "versus / competition format"

        # ── 3. Transcript mining ──────────────────────────────────────────────
        if transcript:
            all_text = " ".join(item.get("text", "") for item in transcript).lower()

            # Count parasocial / relationship signals
            cb_count = sum(1 for p in AUDIENCE_CALLBACK_PHRASES if p in all_text)
            auth_count = sum(1 for p in CREATOR_AUTHENTICITY_MARKERS if p in all_text)
            selfid_count = sum(1 for p in CREATOR_SELFID_PHRASES if p in all_text)
            series_count = sum(1 for p in SERIES_CONTINUITY_SIGNALS if p in all_text)

            ctx["audience_callback_count"] = cb_count
            ctx["authenticity_count"] = auth_count
            ctx["selfid_count"] = selfid_count

            # Determine audience relationship type
            if cb_count >= 3:
                ctx["audience_relationship"] = "community-driven (strong viewer interaction signals)"
            elif selfid_count >= 2 or series_count >= 2:
                ctx["audience_relationship"] = "parasocial (creator builds personal relationship with viewers)"
            elif auth_count >= 2:
                ctx["audience_relationship"] = "authenticity-led (creator shares genuine thoughts and vulnerability)"
            else:
                ctx["audience_relationship"] = "informational / entertainment"

            # Find top repeated multi-word phrases (2-4 word n-grams repeated 3+ times)
            import collections
            words = all_text.split()
            ngram_counts: collections.Counter = collections.Counter()
            for n in (2, 3, 4):
                for i in range(len(words) - n + 1):
                    gram = " ".join(words[i:i+n])
                    # Skip if only stopwords
                    if re.search(r"[a-z]{3,}", gram):
                        ngram_counts[gram] += 1
            top_phrases = [
                phrase for phrase, count in ngram_counts.most_common(20)
                if count >= 3 and len(phrase) > 8
                and not all(w in {"the", "and", "that", "this", "with", "have", "you", "are", "was", "for", "not"} for w in phrase.split())
            ][:3]
            ctx["top_catchphrases"] = top_phrases

        # ── 4. Known Creator DB lookup ────────────────────────────────────────
        creator_db_entry: Optional[Dict[str, Any]] = None
        author_lower = ctx["author_name"].lower().strip()

        # Direct key match first
        if author_lower in KNOWN_CREATOR_DB:
            creator_db_entry = KNOWN_CREATOR_DB[author_lower]
        else:
            # Try alias lookup
            alias_key = _CREATOR_ALIASES.get(author_lower)
            if alias_key and alias_key in KNOWN_CREATOR_DB:
                creator_db_entry = KNOWN_CREATOR_DB[alias_key]
            else:
                # Substring match — author name contains or is contained by a known key
                for db_key, db_val in KNOWN_CREATOR_DB.items():
                    if db_key in author_lower or author_lower in db_key:
                        creator_db_entry = db_val
                        break

        if creator_db_entry:
            ctx["content_niche_override"] = creator_db_entry["content_niche"]
            # Override catchphrases with known ones if transcript mining found nothing
            if not ctx["top_catchphrases"] and creator_db_entry.get("catchphrases"):
                ctx["top_catchphrases"] = creator_db_entry["catchphrases"][:3]
            logger.info(f"[Creator Research] KNOWN CREATOR MATCH: '{creator_db_entry['display_name']}' — using creator DB.")

        # ── 5. Build the Context Card string ─────────────────────────────────
        catchphrase_str = ", ".join(f'"{p}"' for p in ctx["top_catchphrases"]) if ctx["top_catchphrases"] else "none detected"
        personality_signals = []
        if ctx["audience_callback_count"] >= 2:
            personality_signals.append(f"audience callbacks ({ctx['audience_callback_count']}x)")
        if ctx["authenticity_count"] >= 1:
            personality_signals.append(f"vulnerability/authenticity moments ({ctx['authenticity_count']}x)")
        if ctx["selfid_count"] >= 1:
            personality_signals.append(f"direct viewer address ({ctx['selfid_count']}x)")
        personality_str = ", ".join(personality_signals) if personality_signals else "standard delivery"

        if creator_db_entry:
            viral_patterns_str = "\n".join(
                f"    • {p}" for p in creator_db_entry.get("viral_clip_patterns", [])[:4]
            )
            ctx["context_card"] = (
                f"CREATOR RESEARCH (study this carefully before selecting clips):\n"
                f"  Channel Name    : {creator_db_entry['display_name']}\n"
                f"  Video Title     : \"{title}\"\n"
                f"  Career Era      : {creator_db_entry.get('current_career_era', 'Modern Content Era')}\n"
                f"  Video Format    : {ctx['series_signal']}\n"
                f"  Primary Content : {creator_db_entry.get('primary_games', 'variety')}\n"
                f"  Known For       : {creator_db_entry.get('known_for', 'N/A')}\n"
                f"  Catchphrases    : {', '.join(creator_db_entry.get('catchphrases', [])[:5]) or 'none'}\n"
                f"  Viral Patterns  :\n{viral_patterns_str}\n"
                f"  Subscribe Why   : {creator_db_entry.get('subscribe_psychology', 'N/A')}\n"
                f"  Hook Style      : {creator_db_entry.get('hook_style', 'N/A')}\n"
                f"  Audience Bond   : {ctx['audience_relationship']}\n"
            )
        else:
            ctx["context_card"] = (
                f"CREATOR RESEARCH (study this before selecting clips):\n"
                f"  Channel Name  : {ctx['author_name']}\n"
                f"  Video Title   : \"{title}\"\n"
                f"  Video Format  : {ctx['series_signal']}\n"
                f"  Catchphrases  : {catchphrase_str}\n"
                f"  Personality   : {personality_str}\n"
                f"  Audience Bond : {ctx['audience_relationship']}\n"
            )

        logger.info(
            f"[Creator Research] format='{ctx['format_type']}' | "
            f"creator_db={'YES: ' + creator_db_entry.get('display_name', '?') if creator_db_entry else 'NO MATCH'} | "
            f"catchphrases={ctx['top_catchphrases']} | "
            f"callbacks={ctx['audience_callback_count']} auth={ctx['authenticity_count']} | "
            f"relationship='{ctx['audience_relationship']}'"
        )
        return ctx

    def fetch_transcript(self, youtube_url: str) -> List[Dict[str, Any]]:
        """Fetch transcript with timestamps from YouTube Video URL with language fallbacks and yt-dlp VTT backup."""
        video_id = extract_youtube_id(youtube_url)
        if not video_id:
            logger.warning(f"Could not parse YouTube video ID from URL: {youtube_url}")
            return []

        if video_id in _TRANSCRIPT_CACHE:
            logger.info(f"Using cached transcript for YouTube ID: {video_id} ({len(_TRANSCRIPT_CACHE[video_id])} entries)")
            return _TRANSCRIPT_CACHE[video_id]

        logger.info(f"Fetching transcript for YouTube ID: {video_id}")

        # Attempt 1: youtube-transcript-api v0.6.3+ fetch() method with language priority
        try:
            api = YouTubeTranscriptApi()
            languages = ["en", "en-US", "en-GB", "a.en", "en-CA", "en-AU"]
            
            try:
                raw_transcript = api.fetch(video_id, languages=languages)
                snippets = [_normalize_snippet(item) for item in raw_transcript]
                if snippets:
                    logger.info(f"Successfully fetched transcript via YouTubeTranscriptApi().fetch() ({len(snippets)} entries)")
                    _TRANSCRIPT_CACHE[video_id] = snippets
                    return snippets
            except Exception as e:
                logger.debug(f"Direct fetch() with languages failed ({e}). Trying fallback list()...")

            transcript_list = api.list(video_id)
            for tr in transcript_list:
                try:
                    raw = tr.fetch()
                    snippets = [_normalize_snippet(item) for item in raw]
                    if snippets:
                        logger.info(f"Successfully fetched transcript via YouTubeTranscriptApi().list() ({len(snippets)} entries)")
                        _TRANSCRIPT_CACHE[video_id] = snippets
                        return snippets
                except Exception as list_fetch_err:
                    # Bug 12 fix: Log swallowed list transcript fetch exception at debug level
                    logger.debug(f"Transcript list entry fetch failed: {list_fetch_err}")
        except Exception as e:
            logger.debug(f"YouTubeTranscriptApi path failed ({e}). Checking local yt-dlp VTT subtitles...")

        # Attempt 2: Local yt-dlp extracted VTT subtitle files in temp/
        vtt_candidates = list(TEMP_DIR.glob(f"{video_id}*.vtt"))
        for vtt in vtt_candidates:
            snippets = parse_vtt_subtitle(vtt)
            if snippets:
                _TRANSCRIPT_CACHE[video_id] = snippets
                return snippets

        logger.warning(f"All transcript fetching attempts failed for YouTube ID: {video_id}")
        return []

    def analyze_viral_clips(
        self, transcript: List[Dict[str, Any]], video_duration: float, title: str,
        youtube_url: str = ""
    ) -> Tuple[List[Dict[str, Any]], str]:
        if not transcript:
            logger.warning("No transcript available on YouTube; using uniform interval fallback segmentation.")
            raw_clips = self._generate_uniform_fallbacks(video_duration)
            filtered = self.filter_and_rank_candidates(raw_clips, video_duration)
            if not filtered:
                # Guarantee fallback clips pass through
                filtered = raw_clips
            return filtered, "uniform_fallback"

        # ── Creator research phase (runs before any LLM call) ─────────────────
        creator_ctx: Dict[str, Any] = {}
        if youtube_url:
            try:
                creator_ctx = self._research_creator_context(youtube_url, title, transcript)
            except Exception as e:
                logger.warning(f"[Creator Research] Failed gracefully ({e}). Proceeding without context enrichment.")

        if not self.gemini_key:
            logger.error(
                "[FALLBACK REASON] Gemini API Key is not set. "
                "Go to Settings and enter your Gemini API key. "
                "Falling back to heuristic analysis."
            )
        else:
            logger.info(f"[LLM] Using Google Gemini API. Key prefix: {self.gemini_key[:8]}...")
            try:
                raw_clips = self._analyze_with_gemini(transcript, video_duration, title, creator_ctx=creator_ctx)
                filtered = self.filter_and_rank_candidates(raw_clips, video_duration)
                if not filtered:
                    logger.warning(
                        "[FALLBACK REASON] Gemini returned clips but zero passed confidence "
                        f"threshold ({self.min_confidence_threshold}). Falling back to heuristic."
                    )
                else:
                    return filtered, "gemini_transcript"
            except Exception as e:
                logger.error(f"[FALLBACK REASON] Gemini API analysis failed: {e}. Falling back to heuristic analysis.")

        logger.warning("[FALLBACK] Using built-in heuristic analysis engine.")
        raw_clips = self._heuristic_analysis(transcript, video_duration)
        filtered = self.filter_and_rank_candidates(raw_clips, video_duration)
        return filtered, "heuristic_transcript"

    def filter_and_rank_candidates(
        self, raw_clips: List[Dict[str, Any]], video_duration: float = 300.0
    ) -> List[Dict[str, Any]]:
        """Filter candidates by derived confidence score and enforce minimum spacing between start times."""
        valid_candidates = []
        for clip in raw_clips:
            # Normalize timestamps to float seconds (handles HH:MM:SS strings and floats)
            clip["start_time"] = parse_time_to_seconds(clip.get("start_time", 0.0))
            clip["end_time"] = parse_time_to_seconds(clip.get("end_time", 0.0))
            if clip["end_time"] <= clip["start_time"]:
                clip["end_time"] = min(video_duration, clip["start_time"] + 30.0)

            # Map viral_score / confidence if provided in 0-100 scale
            if "viral_score" in clip and "hook_strength" not in clip:
                vs = float(clip.get("viral_score", 70)) / 100.0
                clip["hook_strength"] = vs
                clip["emotional_intensity"] = vs
                clip["unexpectedness"] = vs
                clip["payoff_quality"] = vs

            derived_score = compute_derived_score(clip)
            clip["confidence_score"] = derived_score

            # Guard against null values from LLM JSON for display variables
            hook_str = float(clip.get("hook_strength") or 0.7)
            emo_str = float(clip.get("emotional_intensity") or 0.7)
            unexp_str = float(clip.get("unexpectedness") or 0.7)
            payoff_str = float(clip.get("payoff_quality") or 0.7)

            # Normalize hook_text / title_hook
            if "title_hook" in clip and not clip.get("hook_text"):
                clip["hook_text"] = str(clip["title_hook"]).upper()

            if derived_score >= self.min_confidence_threshold:
                valid_candidates.append(clip)
                logger.info(
                    f"Candidate accepted ({clip.get('start_time'):.1f}s-{clip.get('end_time'):.1f}s): "
                    f"Derived Score={derived_score:.3f} (Hook={hook_str:.2f}, Emotion={emo_str:.2f}, "
                    f"Unexp={unexp_str:.2f}, Payoff={payoff_str:.2f}) | Reasoning: '{clip.get('reasoning', clip.get('why_it_works', ''))[:60]}' | Hook Text: '{clip.get('hook_text', '')}'"
                )
            else:
                logger.info(
                    f"Dropped low-confidence candidate ({clip.get('start_time'):.1f}s): "
                    f"Derived Score {derived_score:.3f} < Threshold {self.min_confidence_threshold}"
                )

        if not valid_candidates:
            logger.warning(f"Zero candidate clips passed confidence threshold >= {self.min_confidence_threshold}")
            return []

        # Sort candidates by derived score descending
        valid_candidates.sort(key=lambda c: float(c.get("confidence_score", 0.7)), reverse=True)

        # Adaptive effective spacing: for shorter videos, allow spacing down to min_clip_dur
        min_bound = min(30.0, float(self.min_clip_dur))
        effective_spacing = min(
            float(self.min_clip_spacing_sec),
            max(min_bound, float(video_duration) / (float(self.target_clip_count) + 1.0))
        )

        accepted_clips = []
        for candidate in valid_candidates:
            c_start = float(candidate["start_time"])

            too_close = False
            for accepted in accepted_clips:
                a_start = float(accepted["start_time"])
                if abs(c_start - a_start) < effective_spacing:
                    too_close = True
                    logger.info(
                        f"Skipping candidate at {c_start:.1f}s (score: {candidate.get('confidence_score')}): "
                        f"too close to accepted clip at {a_start:.1f}s (< {effective_spacing:.1f}s effective spacing)"
                    )
                    break

            if not too_close:
                accepted_clips.append(candidate)
                if len(accepted_clips) >= self.target_clip_count:
                    break

        accepted_clips.sort(key=lambda c: float(c["start_time"]))

        logger.info(
            f"Candidate filter summary: {len(raw_clips)} raw candidates -> "
            f"{len(accepted_clips)} accepted clips (min score: {self.min_confidence_threshold}, "
            f"effective spacing: {effective_spacing:.1f}s, target count: {self.target_clip_count})"
        )
        return accepted_clips

    def _build_prompt(
        self,
        transcript: List[Dict[str, Any]],
        video_duration: float,
        title: str,
        content_type: str = "general",
        creator_ctx: Optional[Dict[str, Any]] = None
    ) -> str:
        """Build Viewer-Psychology Viral Hook Prompt with Creator Research Context Card."""
        max_entries = 1200
        if len(transcript) > max_entries:
            step = len(transcript) / max_entries
            sampled = [transcript[int(i * step)] for i in range(max_entries)]
        else:
            sampled = transcript

        # Annotate transcript with energy markers [🔥 PEAK], [❓ HOOK], [🤝 CONNECT]
        formatted_transcript = []
        for item in sampled:
            text = item.get("text", "")
            text_lower = text.lower()
            tag = ""
            if any(w in text_lower for w in STREAMING_REACTION_WORDS) or "!" in text:
                tag = " [🔥 PEAK]"
            elif any(w in text_lower for w in CURIOSITY_HOOK_TRIGGERS) or "?" in text:
                tag = " [❓ HOOK]"
            elif (any(p in text_lower for p in AUDIENCE_CALLBACK_PHRASES) or
                  any(p in text_lower for p in CREATOR_AUTHENTICITY_MARKERS) or
                  any(p in text_lower for p in CREATOR_SELFID_PHRASES)):
                tag = " [🤝 CONNECT]"
            formatted_transcript.append(f"[{item['start']:.1f}s]{tag} {text}")
        transcript_text = "\n".join(formatted_transcript)

        # Content-specific rules
        content_guidance = ""
        if content_type in ("gaming", "gaming_fps", "gaming_sandbox", "gaming_stream_react"):
            content_guidance = """
GAMING / STREAMING VIEWER PSYCHOLOGY:
- The viewer scrolling past IS a gamer. They recognize rage-quits, clutch moments, and chat chaos instantly.
- HIGHEST VALUE clips: Genuine streamer meltdown, 1v4 clutch they can't believe, hilarious accidental death, chat going absolutely insane.
- FAMILIARITY SIGNAL: Does this clip showcase THIS streamer's unique play style or personality reaction? Not just any gaming moment — THEIR moment.
- SUBSCRIBE TRIGGER: Viewers subscribe when they think "I want to watch this person play more" — not just "that was a cool play".
- Hook: Start 1-2s before the moment. First 3s MUST show the stakes clearly.
- Payoff: End 1s after the scream, victory, or reaction. No dragging."""
        elif content_type == "podcast":
            content_guidance = """
PODCAST / CONVERSATION VIEWER PSYCHOLOGY:
- The viewer is in discovery mode — looking for a host whose perspective they want to adopt.
- HIGHEST VALUE clips: Shocking revelation that reframes their worldview, hot take they agree with but never said out loud, emotional vulnerability that makes the host feel human.
- FAMILIARITY SIGNAL: Does this clip make the viewer feel like they know this person's values and worldview?
- SUBSCRIBE TRIGGER: "I want to hear more of their perspective" — not just "that was interesting".
- Hook: Open on the provocative statement or question. Payoff: mic-drop conclusion."""
        elif content_type == "drama":
            content_guidance = """
DRAMA / COMMENTARY VIEWER PSYCHOLOGY:
- The viewer already heard about this situation. They want receipts, not summaries.
- HIGHEST VALUE clips: The direct callout with evidence, the moment someone gets caught, the instant regret reaction.
- FAMILIARITY SIGNAL: Does this clip give the viewer the satisfaction of having their opinion validated?
- SUBSCRIBE TRIGGER: "This person covers drama I care about AND they don't sugarcoat it".
- Hook: Start on the accusation or shocking quote. Payoff: immediate after verdict."""
        elif content_type in ("irl_stream", "vlog"):
            content_guidance = """
IRL STREAMING & PUBLIC VIRAL MOMENT VIEWER PSYCHOLOGY:
- Viewers watch IRL streams for unscripted tension, public confrontation, unexpected strangers, crowd reactions, and chaotic spontaneity.
- HIGHEST VALUE clips:
  1. Stream sniper or fan approach with hilarious/awkward interaction.
  2. Public confrontation, security/police approaching, or near-ejection moments.
  3. Streamer doing something audacious in a restaurant/store/crowd that breaks social norms.
  4. Streamer being genuinely humbled, caught off guard, or making a stranger laugh.
  5. High-energy group chaos (e.g. FaZe house antics, NYC streets, travelling abroad).
- FAMILIARITY SIGNAL: Does this show the streamer's charisma, signature banter, or how they handle high-pressure real-world situations?
- SUBSCRIBE TRIGGER: "Their IRL streams are completely unpredictable and entertaining — I need to tune in live to the next one."
- Hook: Start on the approach, the tension trigger, or the initial question asked in public. The first 3s must establish what situation they are getting into!
- Payoff: Cut right after the punchline, resolution, or the stranger's reaction."""

        # Creator Context Card (injected if creator research was run)
        context_card_section = ""
        if creator_ctx and creator_ctx.get("context_card"):
            context_card_section = f"""
{creator_ctx['context_card']}
IMPORTANT: Use the creator research above to make your clip selections channel-specific.
A clip that shows THIS creator's unique personality and format will outperform a generic exciting moment every time.
"""

        oversample_count = min(self.target_clip_count * 2, 12)

        prompt = f"""Return ONLY a valid JSON array of objects. Do not include markdown code fences or conversational text.

You are a behavioral psychologist and viral content strategist. You think EXACTLY like a real viewer who has been scrolling Shorts for 30 minutes and has seen 200 clips today. You know precisely what makes a thumb stop, a heart feel something genuine, and a Subscribe button get pressed.

You are NOT picking "exciting moments" — you are picking moments that make a NEW viewer feel an instant connection to THIS creator and want to see more of their content.
{context_card_section}
Video: "{title}" | Duration: {video_duration:.1f}s | Niche: {content_type.upper()}

YOUR MISSION:
Find the TOP {oversample_count} clips ({self.min_clip_dur:.0f}s–{self.max_clip_dur:.0f}s each) that would make a FIRST-TIME viewer of this channel stop scrolling, feel something real, and hit Subscribe.

VIEWER PSYCHOLOGY RULES (ALL MANDATORY):

1. THE FEED STOP RULE (first 2 seconds decide everything):
   - A viewer with their thumb hovering will stop ONLY for something they personally recognize, fear, laugh at, or are shocked by.
   - Generic excitement scrolls past. "BRO WHAT" without context scrolls past. Manufactured drama scrolls past.
   - Ask yourself: would a NEW viewer of this creator's niche STOP here specifically? What makes this the right hook for THIS audience?
   - Set start_time exactly 2 SECONDS BEFORE the best hook line — that brief setup is what makes the payoff land.
   - NEVER start on greetings, dead air, "hey guys", or filler. The first frame must establish stakes.

2. THE FAMILIARITY RULE (earn the subscribe):
   - The clip must feel like meeting a friend, not watching a stranger.
   - Look for moments where THIS creator's specific personality, humor, catchphrase, or unique format shines through.
   - [🤝 CONNECT] marked lines are prime candidates — they show the creator building real audience relationship.
   - A moment that could come from ANY channel is worth less than a moment that is unmistakably THIS channel.

3. THE "WANT MORE" RULE (the subscribe trigger):
   - The payoff must leave a gap. After watching, the viewer thinks: "I NEED to see what happened next / what they do every episode / more of this person."
   - Clips from series, ongoing challenges, or recurring formats are MORE valuable than one-off moments.
   - End the clip IMMEDIATELY after the payoff — never let it drag. The gap is what drives the subscribe.

4. THE AUTHENTICITY RULE (real beats performed):
   - Real vulnerability, genuine surprise, and unscripted reactions outperform polished performance every time.
   - [🤝 CONNECT] lines with "not gonna lie", "honestly", "you guys", "from the comments" signal authentic moments.
   - Avoid clips where the creator seems to be performing for the camera rather than genuinely reacting.

5. THE FLASH-FORWARD HOOK FORMAT (technical requirement):
   - Our editor will show a 2-second preview of the PEAK MOMENT at the very start before rewinding to tell the full story.
   - You MUST identify peak_moment_offset: exact seconds from clip start (NOT video start) where the single most emotionally impactful moment is.
   - Example: start_time=43.0s, climax at video timestamp 67.0s → peak_moment_offset = 67.0 - 43.0 = 24.0
   - Must be between 5.0 and (clip_duration - 3.0) seconds. NOT the very start or very end.

{content_guidance}

Transcript (annotated: [🔥 PEAK]=high energy, [❓ HOOK]=curiosity trigger, [🤝 CONNECT]=audience connection):
{transcript_text}

JSON OUTPUT — Return ONLY a valid JSON array:
[
  {{
    "reasoning": "Why a FIRST-TIME viewer of this channel would stop scrolling, feel something real, and hit Subscribe after watching this specific clip",
    "start_time": 45.2,
    "end_time": 78.5,
    "peak_moment_offset": 24.0,
    "hook_strength": 0.95,
    "emotional_intensity": 0.90,
    "unexpectedness": 0.85,
    "payoff_quality": 0.92,
    "hook_text": "ALL-CAPS HOOK THAT SPEAKS DIRECTLY TO THIS CREATOR'S AUDIENCE",
    "subscribe_trigger": "One sentence: what makes a new viewer hit subscribe after this clip",
    "familiarity_signal": "What THIS creator's unique personality/format this clip showcases",
    "viewer_feeling": "The exact emotion a new viewer feels in the first 3 seconds",
    "parasocial_score": 0.88,
    "series_bait": true,
    "caption": "Short caption for TikTok/Shorts",
    "tags": "#shorts #viral",
    "explanation": "One sentence summary"
  }}
]

Scoring Metrics (0.0 to 1.0) — think like a viewer, not a producer:
- hook_strength: Would THIS creator's specific audience STOP scrolling here? (0.90+ = instant freeze)
- emotional_intensity: How REAL and authentic does the emotion feel? (high = genuine, not performed)
- unexpectedness: Does this subvert what viewers expect from THIS creator's normal content format?
- payoff_quality: Does the ending give a complete story arc AND leave the viewer wanting the next episode?
- parasocial_score: How strongly does this make viewers feel they KNOW this creator personally? (0.0–1.0)
- series_bait: true if this clip makes viewers want to follow the channel's ongoing content/format

peak_moment_offset: Seconds from clip start to the single most emotionally impactful moment (the scream, crash, punchline, or reveal that will be flash-previewed at the start).

Return ONLY the JSON array."""
        return prompt



    def _parse_json_response(self, response_text: str) -> List[Dict[str, Any]]:
        """Shared JSON extraction and validation for LLM responses with null/type guards."""
        if not response_text or not isinstance(response_text, str) or not response_text.strip():
            raise ValueError("Cannot parse JSON from empty or non-string response text.")

        clean_text = response_text.strip()
        clips = None

        # Strategy 1: Extract from markdown code fence ```json [...] ``` anywhere in text
        fence_match = re.search(r"```(?:json)?\s*([\[\{][\s\S]*?[\]\}])\s*```", clean_text)
        if fence_match:
            try:
                candidate = json.loads(fence_match.group(1))
                if isinstance(candidate, (list, dict)):
                    clips = candidate
            except Exception:
                pass

        # Strategy 2: Bracket-to-bracket direct slice for JSON array
        if clips is None:
            first_b = clean_text.find("[")
            last_b = clean_text.rfind("]")
            if first_b != -1 and last_b > first_b:
                try:
                    candidate = json.loads(clean_text[first_b : last_b + 1])
                    if isinstance(candidate, list):
                        clips = candidate
                except Exception:
                    pass

        # Strategy 3: Balanced stack JSON substring extractor
        if clips is None:
            extracted = _extract_json_substring(clean_text)
            if extracted:
                try:
                    candidate = json.loads(extracted)
                    if isinstance(candidate, (list, dict)):
                        clips = candidate
                except Exception:
                    pass

        # Strategy 4: Truncated JSON array salvage (e.g. when LLM reaches max_tokens mid-candidate)
        if clips is None:
            first_bracket = clean_text.find("[")
            last_brace = clean_text.rfind("}")
            if first_bracket != -1 and last_brace > first_bracket:
                repaired_core = clean_text[first_bracket : last_brace + 1].rstrip(", \r\n\t")
                repaired = repaired_core + "\n]"
                try:
                    candidate = json.loads(repaired)
                    if isinstance(candidate, list):
                        clips = candidate
                        logger.info(f"Successfully salvaged {len(clips)} candidate clips from truncated JSON response.")
                except Exception:
                    pass

        # Strategy 5: Object wrapper with curly braces
        if clips is None:
            first_brace = clean_text.find("{")
            last_brace = clean_text.rfind("}")
            if first_brace != -1 and last_brace > first_brace:
                try:
                    candidate = json.loads(clean_text[first_brace : last_brace + 1])
                    if isinstance(candidate, dict):
                        clips = candidate
                except Exception:
                    pass

        if clips is None:
            raise ValueError(f"Could not parse JSON array from response: {clean_text[:100]}")

        # Safe extraction if model returned a wrapper dictionary
        if isinstance(clips, dict):
            for candidate_key in ["clips", "candidates", "results", "moments", "data"]:
                if candidate_key in clips and isinstance(clips[candidate_key], list):
                    clips = clips[candidate_key]
                    break
            else:
                first_list_val = next((v for v in clips.values() if isinstance(v, list)), [])
                clips = first_list_val

        return clips if isinstance(clips, list) else []

    def _analyze_with_gemini(
        self, transcript: List[Dict[str, Any]], video_duration: float, title: str,
        creator_ctx: Optional[Dict[str, Any]] = None
    ) -> List[Dict[str, Any]]:
        """Call Google Gemini API using primary model with automatic fallback to fast secondary models."""
        content_type = (creator_ctx.get("content_niche_override") if creator_ctx else None) or _detect_content_type(transcript)
        prompt = self._build_prompt(transcript, video_duration, title, content_type=content_type, creator_ctx=creator_ctx)

        candidate_models = [self.model]
        for fb in FALLBACK_GEMINI_MODELS:
            if fb not in candidate_models:
                candidate_models.append(fb)

        last_error = None
        for model_name in candidate_models:
            logger.info(f"Sending transcript to Google Gemini API (model: {model_name})...")
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={self.gemini_key}"
            payload = json.dumps({
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {
                    "temperature": 0.35,
                    "maxOutputTokens": 8192,
                    "responseMimeType": "application/json"
                }
            }).encode("utf-8")

            try:
                req = urllib.request.Request(
                    url,
                    data=payload,
                    headers={
                        "Content-Type": "application/json",
                        "x-goog-api-key": self.gemini_key,
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AI-Clipper/10.0"
                    }
                )
                with urllib.request.urlopen(req, timeout=45) as resp:
                    res_body = json.loads(resp.read().decode("utf-8"))

                candidates = res_body.get("candidates", [])
                parts = candidates[0].get("content", {}).get("parts", []) if candidates else []
                raw_text = parts[0].get("text") if parts else None

                if not raw_text or not isinstance(raw_text, str) or not raw_text.strip():
                    raise RuntimeError(f"Gemini model '{model_name}' returned empty/null content.")

                clips = self._parse_json_response(raw_text)
                if clips:
                    logger.info(f"Google Gemini ({model_name}) identified {len(clips)} viral candidate clips.")
                    return clips
            except Exception as e:
                logger.warning(f"Google Gemini model '{model_name}' failed ({e}). Trying next model...")
                last_error = e

        raise RuntimeError(f"All Google Gemini models failed. Last error: {last_error}")

    def _heuristic_analysis(
        self, transcript: List[Dict[str, Any]], video_duration: float
    ) -> List[Dict[str, Any]]:
        """Pro-level multi-signal offline heuristic analysis engine.
        
        Evaluates 6 retention signals across sliding windows:
        1. Reaction Word Density (25%)
        2. Curiosity / Question Density (20%)
        3. Exclamation / High-Energy Caps (15%)
        4. Rapid Reaction Speech Rhythm (10%)
        5. Clustered Keyword Surges (15%)
        6. Word Richness (15%)
        """
        if not transcript:
            return self._generate_uniform_fallbacks(video_duration)

        content_type = _detect_content_type(transcript)

        effective_spacing = min(
            float(self.min_clip_spacing_sec),
            max(30.0, float(video_duration) / (float(self.target_clip_count) + 1.0))
        )

        # Build sliding windows of ~15 seconds across transcript
        scored_windows = []
        window_duration = 15.0
        
        t_max = max(float(item.get("start", 0)) for item in transcript)
        step_sec = 5.0
        current_t = 0.0

        while current_t < t_max:
            w_start = current_t
            w_end = current_t + window_duration
            
            window_entries = [
                e for e in transcript
                if w_start <= float(e.get("start", 0)) < w_end
            ]
            
            if window_entries:
                full_w_text = " ".join(e.get("text", "") for e in window_entries)
                w_lower = full_w_text.lower()
                
                # Signal 1: Reaction Word Density (25%)
                reaction_count = sum(1 for w in STREAMING_REACTION_WORDS if w in w_lower)
                sig_reaction = min(1.0, reaction_count * 0.35)
                
                # Signal 2: Curiosity & Question Triggers (20%)
                q_count = full_w_text.count("?")
                hook_trig_count = sum(1 for h in CURIOSITY_HOOK_TRIGGERS if h in w_lower)
                sig_curiosity = min(1.0, q_count * 0.40 + hook_trig_count * 0.30)
                
                # Signal 3: Exclamations & High-Energy ALL-CAPS (15%)
                exclam_count = full_w_text.count("!")
                words = full_w_text.split()
                caps_count = sum(1 for w in words if w.isupper() and len(w) > 1)
                sig_energy = min(1.0, exclam_count * 0.30 + caps_count * 0.15)
                
                # Signal 4: Rapid Speech / Short Burst Rhythm (10%)
                avg_dur = sum(float(e.get("duration", 2)) for e in window_entries) / len(window_entries)
                sig_rhythm = 0.8 if avg_dur < 1.5 else (0.5 if avg_dur < 2.5 else 0.3)
                
                # Signal 5: Clustered Keyword Surges (15%)
                sig_surge = 1.0 if (reaction_count + hook_trig_count + exclam_count) >= 3 else 0.4
                
                # Signal 6: Word Richness (15%)
                sig_richness = min(1.0, len(words) / 30.0)

                composite_score = (
                    0.25 * sig_reaction +
                    0.20 * sig_curiosity +
                    0.15 * sig_energy +
                    0.10 * sig_rhythm +
                    0.15 * sig_surge +
                    0.15 * sig_richness
                )

                first_entry_text = window_entries[0].get("text", "")
                scored_windows.append({
                    "start_t": float(window_entries[0].get("start", w_start)),
                    "score": composite_score,
                    "hook_text": _build_hook_from_text(first_entry_text),
                    "full_text": full_w_text[:80],
                    "reaction_count": reaction_count,
                    "has_exclam": exclam_count > 0 or q_count > 0,
                })

            current_t += step_sec

        logger.info(f"[Heuristic Multi-Signal] Evaluated {len(scored_windows)} sliding windows across transcript.")

        # Sort windows by score descending
        scored_windows.sort(key=lambda w: w["score"], reverse=True)

        # Enforce effective spacing between selected candidate moments
        selected_windows = []
        for w in scored_windows:
            w_t = w["start_t"]
            too_close = any(abs(w_t - s["start_t"]) < effective_spacing for s in selected_windows)
            if not too_close:
                selected_windows.append(w)
                if len(selected_windows) >= self.target_clip_count * 2:
                    break

        candidates: List[Dict[str, Any]] = []
        for idx, win in enumerate(selected_windows):
            peak_t = win["start_t"]
            # Start clip exactly 2 seconds before the hook peak (matching 2-second pre-hook rule)
            clip_start = max(0.0, peak_t - 2.0)
            clip_end = min(video_duration, clip_start + 38.0)
            
            raw_score = win["score"]
            hook_str = min(0.96, 0.75 + raw_score * 0.22 + (0.05 if win["has_exclam"] else 0.0))
            emo_str = min(0.95, 0.72 + raw_score * 0.24)
            unexp_str = min(0.92, 0.70 + raw_score * 0.20)
            payoff_str = 0.88

            candidates.append({
                "reasoning": f"Multi-signal viral peak at {peak_t:.1f}s (score={raw_score:.2f}, niche={content_type}): '{win['full_text']}'",
                "start_time": clip_start,
                "end_time": clip_end,
                "hook_strength": round(hook_str, 2),
                "emotional_intensity": round(emo_str, 2),
                "unexpectedness": round(unexp_str, 2),
                "payoff_quality": round(payoff_str, 2),
                "hook_text": win["hook_text"],
                "explanation": f"Viral moment at {int(peak_t)}s ({content_type}).",
                "caption": win["hook_text"],
                "tags": f"#shorts #viral #{content_type}",
            })

        # If needed, fill remaining slots with spaced stride candidates
        if len(candidates) < self.target_clip_count:
            total_entries = len(transcript)
            stride = max(1, total_entries // (self.target_clip_count + 1))
            for idx in range(stride, total_entries - stride, stride):
                if len(candidates) >= self.target_clip_count * 2:
                    break
                entry = transcript[idx]
                start_t = float(entry["start"])
                too_close = any(abs(start_t - c["start_time"]) < effective_spacing for c in candidates)
                if not too_close:
                    text_snip = entry.get("text", "").strip()
                    candidates.append({
                        "reasoning": f"Stride backup candidate at {start_t:.1f}s",
                        "start_time": max(0.0, start_t - 1.0),
                        "end_time": min(video_duration, start_t + 35.0),
                        "hook_strength": 0.75,
                        "emotional_intensity": 0.72,
                        "unexpectedness": 0.70,
                        "payoff_quality": 0.80,
                        "hook_text": _build_hook_from_text(text_snip) if text_snip else f"MOMENT AT {int(start_t)}S",
                        "explanation": f"Key segment at {int(start_t)}s.",
                        "caption": "Watch this moment!",
                        "tags": "#shorts #viral",
                    })

        return candidates

    def _generate_uniform_fallbacks(self, video_duration: float) -> List[Dict[str, Any]]:
        """Uniform interval fallback segmentation for uncaptioned videos."""
        interval = max(30.0, video_duration / (self.target_clip_count + 1))
        fallback_score = max(0.85, round(self.min_confidence_threshold + 0.05, 2))
        clips = []
        for i in range(1, self.target_clip_count + 1):
            start_t = i * interval
            end_t = min(video_duration, start_t + 35.0)
            clips.append({
                "reasoning": f"Uniform interval segment {i} of {self.target_clip_count}.",
                "start_time": start_t,
                "end_time": end_t,
                "hook_strength": fallback_score,
                "emotional_intensity": fallback_score,
                "unexpectedness": fallback_score,
                "payoff_quality": fallback_score,
                "confidence_score": fallback_score,
                "hook_text": f"HIGHLIGHT MOMENT {i}",
                "explanation": f"Uniform interval fallback clip {i}",
                "caption": f"Part {i}",
                "tags": "#shorts #viral",
            })
        return clips
