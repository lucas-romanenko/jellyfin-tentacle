"""What each person watches with: subtitle languages and when they show,
and what the Skip intro / Skip credits buttons do.

Each person sets theirs on their own page (Subtitles & skipping); until
they do, the household's starting point applies (Server → People: one for
grown-ups, one for kids). Saved in plain columns on `tentacle_users`
(None = the starting point) and the starting points in settings.

Jellyfin keeps a subtitle language and mode per user, and every Jellyfin
app obeys them: they are written there too (`sync_to_jellyfin`), so the
person's first language and their "when" reach apps Tentacle never touches.
Jellyfin has room for one language; the others reach the menus through the
plugin (it shows only the person's languages), which reads `for_person`.
"""
import logging
from typing import Optional

from models.database import TentacleUser, get_setting, set_setting

logger = logging.getLogger(__name__)

SUBTITLE_MODES = ("off", "always", "foreign")
SKIP_MODES = ("button", "auto", "off")

# The household's starting point when nobody changed anything (the brief:
# English, off until switched on; the skip buttons shown).
BUILTIN = {"subtitle_languages": ["eng"], "subtitle_mode": "off", "skip_intro": "button", "skip_credits": "button"}
BUILTIN_KIDS = {"subtitle_languages": ["eng"], "subtitle_mode": "off", "skip_intro": "auto", "skip_credits": "button"}
_DEFAULT_KEYS = {False: "watch_defaults_", True: "watch_defaults_kids_"}
FIELDS = ("subtitle_languages", "subtitle_mode", "skip_intro", "skip_credits")

# ISO 639-2 codes people pick from: (code, English name, own name). The
# bibliographic code (what Matroska files carry: "fre", "ger"); the
# terminology one a file may carry instead ("fra", "deu") is folded in by
# `normalize`.
LANGUAGES = [
    ("eng", "English", "English"), ("fre", "French", "Français"), ("spa", "Spanish", "Español"),
    ("ger", "German", "Deutsch"), ("ita", "Italian", "Italiano"), ("por", "Portuguese", "Português"),
    ("dut", "Dutch", "Nederlands"), ("rus", "Russian", "Русский"), ("ukr", "Ukrainian", "Українська"),
    ("pol", "Polish", "Polski"), ("cze", "Czech", "Čeština"), ("slo", "Slovak", "Slovenčina"),
    ("hun", "Hungarian", "Magyar"), ("rum", "Romanian", "Română"), ("bul", "Bulgarian", "Български"),
    ("gre", "Greek", "Ελληνικά"), ("tur", "Turkish", "Türkçe"), ("ara", "Arabic", "العربية"),
    ("heb", "Hebrew", "עברית"), ("per", "Persian", "فارسی"), ("hin", "Hindi", "हिन्दी"),
    ("urd", "Urdu", "اردو"), ("ben", "Bengali", "বাংলা"), ("tam", "Tamil", "தமிழ்"),
    ("tel", "Telugu", "తెలుగు"), ("chi", "Chinese", "中文"), ("jpn", "Japanese", "日本語"),
    ("kor", "Korean", "한국어"), ("vie", "Vietnamese", "Tiếng Việt"), ("tha", "Thai", "ไทย"),
    ("ind", "Indonesian", "Bahasa Indonesia"), ("may", "Malay", "Bahasa Melayu"),
    ("fil", "Filipino", "Filipino"), ("swe", "Swedish", "Svenska"), ("nor", "Norwegian", "Norsk"),
    ("dan", "Danish", "Dansk"), ("fin", "Finnish", "Suomi"), ("ice", "Icelandic", "Íslenska"),
    ("hrv", "Croatian", "Hrvatski"), ("srp", "Serbian", "Српски"), ("slv", "Slovenian", "Slovenščina"),
    ("est", "Estonian", "Eesti"), ("lav", "Latvian", "Latviešu"), ("lit", "Lithuanian", "Lietuvių"),
    ("cat", "Catalan", "Català"), ("baq", "Basque", "Euskara"), ("glg", "Galician", "Galego"),
    ("wel", "Welsh", "Cymraeg"), ("gle", "Irish", "Gaeilge"), ("afr", "Afrikaans", "Afrikaans"),
    ("swa", "Swahili", "Kiswahili"),
]
NAMES = {code: name for code, name, _ in LANGUAGES}
_T_TO_B = {"fra": "fre", "deu": "ger", "nld": "dut", "ces": "cze", "slk": "slo", "ron": "rum", "ell": "gre",
           "fas": "per", "zho": "chi", "msa": "may", "isl": "ice", "eus": "baq", "cym": "wel", "sqi": "alb",
           "hye": "arm", "kat": "geo", "mkd": "mac", "mri": "mao", "mya": "bur", "bod": "tib"}
_TWO = {"en": "eng", "fr": "fre", "es": "spa", "de": "ger", "it": "ita", "pt": "por", "nl": "dut", "ru": "rus",
        "uk": "ukr", "pl": "pol", "cs": "cze", "sk": "slo", "hu": "hun", "ro": "rum", "bg": "bul", "el": "gre",
        "tr": "tur", "ar": "ara", "he": "heb", "fa": "per", "hi": "hin", "ur": "urd", "bn": "ben", "ta": "tam",
        "te": "tel", "zh": "chi", "ja": "jpn", "ko": "kor", "vi": "vie", "th": "tha", "id": "ind", "ms": "may",
        "sv": "swe", "no": "nor", "nb": "nor", "nn": "nor", "da": "dan", "fi": "fin", "is": "ice", "hr": "hrv",
        "sr": "srp", "sl": "slv", "et": "est", "lv": "lav", "lt": "lit", "ca": "cat", "eu": "baq", "gl": "glg",
        "cy": "wel", "ga": "gle", "af": "afr", "sw": "swa", "tl": "fil"}
TWO_LETTER = {three: two for two, three in _TWO.items() if two not in ("nb", "nn", "tl")}


def normalize(code: Optional[str]) -> str:
    """A language tag as a file, Jellyfin or a person gives it ("en",
    "eng", "fra", "pt-BR") as one ISO 639-2/B code; "" when unknown."""
    c = (code or "").strip().lower().replace("_", "-").split("-")[0]
    if len(c) == 2:
        return _TWO.get(c, "")
    if len(c) == 3:
        return _T_TO_B.get(c, c) if c not in ("und", "mis", "mul", "zxx") else ""
    return ""


def name(code: str) -> str:
    return NAMES.get(code, code)


def _clean_languages(value) -> list:
    if isinstance(value, str):
        value = value.split(",")
    out = []
    for v in value or []:
        c = normalize(str(v))
        if c and c not in out:
            out.append(c)
    return out


def defaults(db, kids: bool = False) -> dict:
    """What people start with (kids: what kids start with)."""
    base = BUILTIN_KIDS if kids else BUILTIN
    prefix = _DEFAULT_KEYS[kids]
    langs = _clean_languages(get_setting(db, prefix + "subtitle_languages", "") or "") or list(base["subtitle_languages"])
    out = {"subtitle_languages": langs}
    for field, allowed in (("subtitle_mode", SUBTITLE_MODES), ("skip_intro", SKIP_MODES), ("skip_credits", SKIP_MODES)):
        v = (get_setting(db, prefix + field, "") or "").strip()
        out[field] = v if v in allowed else base[field]
    return out


def validate(change: dict) -> dict:
    """The fields of `change` that are set, checked; ValueError (in words
    for a person) when one isn't one of the choices."""
    out = {}
    if "subtitle_languages" in change:
        langs = _clean_languages(change["subtitle_languages"])
        if not langs:
            raise ValueError("Pick at least one language.")
        out["subtitle_languages"] = langs
    for field, allowed in (("subtitle_mode", SUBTITLE_MODES), ("skip_intro", SKIP_MODES), ("skip_credits", SKIP_MODES)):
        if field in change:
            if change[field] not in allowed:
                raise ValueError("That isn't one of the choices.")
            out[field] = change[field]
    return out


def save_defaults(db, change: dict, kids: bool = False) -> dict:
    prefix = _DEFAULT_KEYS[kids]
    for field, value in validate(change).items():
        set_setting(db, prefix + field, ",".join(value) if field == "subtitle_languages" else value)
    return defaults(db, kids)


def for_person(db, user: TentacleUser) -> dict:
    """Their settings, the starting point filling in what they never set;
    `own` says which fields they set themselves."""
    start = defaults(db, kids=bool(user.kids_level))
    langs = _clean_languages(user.subtitle_languages or "")
    out = {"subtitle_languages": langs or start["subtitle_languages"]}
    own = {"subtitle_languages": bool(langs)}
    for field in ("subtitle_mode", "skip_intro", "skip_credits"):
        v = getattr(user, field, None)
        out[field] = v if v else start[field]
        own[field] = bool(v)
    out["own"] = own
    return out


def save_person(db, user: TentacleUser, change: dict) -> dict:
    for field, value in validate(change).items():
        setattr(user, field, ",".join(value) if field == "subtitle_languages" else value)
    db.commit()
    return for_person(db, user)


def reset_person(db, user: TentacleUser) -> dict:
    for field in FIELDS:
        setattr(user, field, None)
    db.commit()
    return for_person(db, user)


def household_languages(db) -> list:
    """Every language anyone in the household reads subtitles in: what the
    extraction keeps (never all 48 tracks of a file)."""
    out = []
    users = db.query(TentacleUser).all()
    for user in users:
        for code in for_person(db, user)["subtitle_languages"]:
            if code not in out:
                out.append(code)
    if not users:
        out = list(defaults(db)["subtitle_languages"])
    return out


# Jellyfin's per-user SubtitleMode for each "when".
JELLYFIN_MODE = {"off": "None", "always": "Always", "foreign": "Smart"}


def jellyfin_configuration(current: dict, prefs: dict) -> dict:
    """Jellyfin's user Configuration with the person's subtitle choice in
    it: their first language, and when ("only when the audio isn't in my
    language" is Jellyfin's Smart, which compares with the audio language
    preference: set to that language when they had none)."""
    conf = dict(current or {})
    first = prefs["subtitle_languages"][0]
    conf["SubtitleLanguagePreference"] = first
    conf["SubtitleMode"] = JELLYFIN_MODE[prefs["subtitle_mode"]]
    if prefs["subtitle_mode"] == "foreign" and not (conf.get("AudioLanguagePreference") or "").strip():
        conf["AudioLanguagePreference"] = first
    return conf


def sync_to_jellyfin(db, user: TentacleUser) -> bool:
    """Write the person's subtitle language and mode into their Jellyfin
    user configuration. False when Jellyfin couldn't be reached (their
    Tentacle settings stand; the next save or start-up tries again)."""
    import requests
    url = (get_setting(db, "jellyfin_url", "") or "").rstrip("/")
    key = get_setting(db, "jellyfin_api_key", "") or ""
    if not url or not key or not user.jellyfin_user_id:
        return False
    headers = {"X-Emby-Token": key}
    try:
        r = requests.get(f"{url}/Users/{user.jellyfin_user_id}", headers=headers, timeout=10)
        r.raise_for_status()
        current = r.json().get("Configuration") or {}
        wanted = jellyfin_configuration(current, for_person(db, user))
        if wanted == current:
            return True
        r = requests.post(f"{url}/Users/{user.jellyfin_user_id}/Configuration", json=wanted, headers=headers, timeout=10)
        r.raise_for_status()
        return True
    except Exception as e:
        logger.warning(f"[Watch] {user.display_name}'s subtitle choice not written to Jellyfin: {type(e).__name__}")
        return False


def sync_everyone(db) -> int:
    n = 0
    for user in db.query(TentacleUser).all():
        n += bool(sync_to_jellyfin(db, user))
    return n
