from functools import lru_cache
import math, re

SECRET_PATTERN = re.compile(
    # private key blocks; a block cut off before its END line is masked to the end of the text
    r"(?:-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----(?:[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----|[\s\S]*)|"
    r"sk-ant-[A-Za-z0-9_\-]+|"
    r"sk-proj-[A-Za-z0-9_\-]+|"
    r"sk-[A-Za-z0-9_\-]{16,}|"
    r"gh[pousr]_[A-Za-z0-9_]+|"
    r"github_pat_[A-Za-z0-9_]+|"
    r"hf_[A-Za-z0-9]{30,}|"
    r"AKIA[0-9A-Z]{16}|"
    r"AIza[0-9A-Za-z\-_]{35}|"
    r"xox[baprs]-[0-9a-zA-Z\-]+|"
    r"eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+|"  # JWT
    r"(?i:bearer)\s+[A-Za-z0-9_\-\.=]{8,}|"
    r"(?<=://)[^/\s:@]+:[^/\s@]+(?=@)|"  # user:password@ in URLs
    # key = value, also JSON-quoted ("password": "x") and env style (AWS_SECRET_ACCESS_KEY=x)
    # (start only at a word start: retrying inside long word runs was quadratic on big tool outputs)
    r"(?<![\w.-])(?i:[\w.-]*(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key))"
    r"\\?[\"']?\s*[:=]\s*\\?[\"']?[^\s\"'\\,;]{4,})"
)

def tr_lower(s: str) -> str:
    return s.replace("I", "ı").replace("İ", "i").lower()

FOLD = str.maketrans("çğıöşü", "cgiosu")

def normalize(s: str) -> str:
    return "" if not s else s.lower() if s.isascii() else tr_lower(s).replace("ı", "i")  # ASCII: I -> ı -> i is plain lower()

@lru_cache(maxsize=4096)
def fold_tr(s: str) -> str:
    return normalize(s).translate(FOLD) if s else ""

COMMON_SUFFIXES = list(dict.fromkeys(normalize(s) for s in (
    "lardan lerden ların lerin larda lerde ından inden undan ünden sının sinin sunun sünün sından sinden sundan sünden "
    "ıyoruz iyoruz uyoruz üyoruz ıyorum iyorum uyorum üyorum sına sine suna süne sını sini sunu sünü "
    "lar ler dan den tan ten nın nin nun nün nda nde nta nte yla yle mız miz muz müz nız niz nuz nüz "
    "dık dik duk dük tık tik tuk tük ıyor iyor uyor üyor acak ecek mak mek mış miş muş müş dır dir dur dür tır tir tur tür "
    "ations ation ments ment ings ing da de ta te na ne ya ye la le nı ni nu nü an en "
    "sı si su sü ın in un ün ım im um üm dı di du dü tı ti tu tü ma me ıl il ul ül ed es er ly a e ı i u ü s n"
).split()))

# Only Turkish stopwords take suffixes, so only these are also matched against a word's stems.
TR_STOP_WORDS = {normalize(w) for w in (
    "mi mı mu mü mıydı miydi muydu müydü mıdır midir mudur müdür ve veya ile için göre kadar "
    "bu şu o bir de da ki ne nedir neydi nelerdir neler neyi neye neyden nere nerede neresi nereye nereden neredeydi neredendir "
    "neden nasıl nasıldı nasıldır niye niçin kim kimdir kimdi kaç kaçtır kaçtı kaça kaçtan "
    "hangi hangisi hangisidir hangileri hangisine acaba ilk önce sonra başta var yok olan olarak ama fakat ancak gibi ise zaman"
).split()}
STOP_WORDS = TR_STOP_WORDS | {normalize(w) for w in (
    "a an the and or not near in on at to for of with by from as into about is are was were be been "
    "do does did have has had what which where when who whom whose why how many much this that these those "
    "it its we our you your can could would should will use used uses using"
).split()}

KIND_LABELS = {
    "decision": "karar decision",
    "failed_attempt": "denendi bırakıldı failed attempt",
    "todo": "yapılacak todo",
    "fact": "bilgi fact",
    "handoff": "handoff devir teslim",
}

SOFT_PAIRS = tuple(map(tuple, "bp dt ğk gk cç pb td kğ kg çc".split()))

@lru_cache(maxsize=4096)
def _stem_norm(w: str) -> tuple[str, ...]:
    res = [w]
    for suf in COMMON_SUFFIXES:
        min_stem = 4 if suf in ("s", "an", "en") else 3
        if len(w) - len(suf) >= min_stem and w.endswith(suf):
            if (stem := w[:-len(suf)]) not in res:
                res.append(stem)
    extras = []
    for s in res:
        if len(s) >= 4:
            for c_from, c_to in SOFT_PAIRS:
                if s.endswith(c_from) and (cand := s[:-1] + c_to) not in res and cand not in extras:
                    extras.append(cand)
    return tuple(res + extras)

def tr_stem(w: str) -> list[str]:
    return list(_stem_norm(normalize(w)))

def trim_tool_output(s: str, body: bool = False) -> str:
    s = mask_secrets(s)  # mask first: a cut could leave part of a secret that no longer matches a pattern
    cut = f"{s[:400]}\n[... {len(s) - 800} chars cut ...]\n{s[-400:]}" if body else s[:1200] + "\n...\n" + s[-800:]
    return cut if len(cut) < len(s) else s  # a marker must never make a short text longer

def split_text(text: str, max_tokens: int = 250) -> list[str]:
    fits = lambda s: math.ceil(len(s) / 4) <= max_tokens

    def _split(block: str, delims: list[str]) -> list[str]:
        if fits(block):
            return [block] if block.strip() else []
        if not delims:
            step = max_tokens * 4
            return [p for i in range(0, len(block), step) if (p := block[i:i + step].strip())]
        sep, out, cur = delims[0], [], None  # cur: the piece being filled; "" is a real (empty) start
        for p in block.split(sep):
            if cur is not None and not fits(f"{cur}{sep}{p}"):
                out += [c] if (c := cur.strip()) else []
                cur = None
            if fits(p):
                cur = p if cur is None else f"{cur}{sep}{p}"
            else:
                out.extend(_split(p, delims[1:]))
        return out + ([c] if (c := (cur or "").strip()) else [])

    return _split(text.strip(), ["\n\n", "\n", " "])

def wrap_untrusted(text: str) -> str:
    # Stored text must not be able to close (or reopen) the wrapper.
    text = re.sub(r"(?i)<(/?)(untrusted_memory)", r"&lt;\1\2", text.strip())
    return f"<untrusted_memory>\n{text}\n</untrusted_memory>"

def mask_secrets(text: str) -> str:
    return SECRET_PATTERN.sub("[REDACTED]", text) if text else text
