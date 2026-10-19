"""Deterministic intent detection on the USER's prompt text (never on model output).

* git_grants(prompt)  -> categories the user explicitly asked for in THIS prompt:
      commit, push, force, amend, branch, pr, gh_write, discard, sync, tag, stash, hard_reset
  Mentions are not requests: "son commit'te ne değişti?", "commit mesajı öner",
  "commitleme", "don't push" grant nothing.
* approval(prompt)    -> "approve" | "reject" | None, used only while a plan awaits approval.
"""
from __future__ import annotations

import re
from typing import List, Optional, Set

from .messages import protocol as _protocol
from .util import norm_lower


class _LazyRe:
    """`re.compile(...)` done on first use. The vocabulary below is ~40 large IGNORECASE patterns (8 ms to compile):
    a hook compiles only the few its event needs, not all of them at start-up."""
    __slots__ = ("_args", "_rx")

    def __init__(self, *args):
        self._args = args
        self._rx = None

    def __getattr__(self, name):
        rx = self._rx
        if rx is None:
            rx = self._rx = re.compile(*self._args)
        return getattr(rx, name)


_NOUNISH = (r"(?!\s*(mesaj|message|msg|hash|id\b|geçmiş|history|log|sayısı|count|listesi|list|bildirim|notification|"
            r"servis|service|server|sunucu|token|izni|permission|'?(i|ı|u|ü)n\b|'?te\b|'?de\b|'?ler))")
_QUESTION_TAIL = _LazyRe(r"\b(commit|komit|push)\w*\s+(mı|mi|mu|mü)\b|\b(commit|komit|push)'?l(e|a)(n)?d(ı|i)(n|niz)?\s*m(ı|i)", re.I)
_PLURAL = _LazyRe(r"\b(commit|komit)'?ler\w*|\bpush'?lar\b", re.I)
_LEAD = r"(^|[.!?,;:]\s*|\b(please|and|then|now|also|just|only|ve|sonra|lütfen|şimdi|hemen|sadece|yalnızca|bir\s+de|da|de)\s+)"

_COMMIT_POS = _LazyRe(
    r"\b(commit|komit)'?ler\s*m[ıi]s[ıi]n\w*"
    r"|\b(commit|komit)'?le(?!n?m[ae]|r\b|ri\b|rin|re\b|rde|rden|ri\w)\w*"   # commitle, commitleyip (not commitler/-leri)
    r"|\b(commit|komit)\s+(at|et|yap|oluştur|create)(?!m[ae])\b"  # commit at / commit et
    r"|\bcommit\s+(it|this|that|these|them|all|everything|the\s+changes|changes|my\s+changes|your\s+changes|the\s+fix|the\s+work)\b"
    r"|" + _LEAD + r"commit\b" + _NOUNISH +
    r"|\bgit\s+commit\b", re.I)
_COMMIT_NEG = _LazyRe(r"\b(commit|komit)'?(le)?n?m[ae]\w*|\b(commit|komit)\s+(atma|etme|yapma)\w*", re.I)

_PUSH_POS = _LazyRe(
    r"\bpush'?lar\s*m[ıi]s[ıi]n\w*|\bpush'?la(?!n?m[ae]|r\b|rı|rın)\w*"
    r"|\bpush\s+(et|yap|at)(?!m[ae])\b"
    r"|\bpush\s+(it|this|that|them|the\s+changes|changes|everything|to\s+\w+|origin|upstream)\b"
    r"|" + _LEAD + r"push\b" + _NOUNISH +
    r"|\bgit\s+push\b", re.I)
_PUSH_NEG = _LazyRe(r"\bpush'?(la)?n?m[ae]\w*|\bpush\s+(etme|yapma|atma)\w*", re.I)

_NEG_EN = _LazyRe(r"\b(don'?t|do\s+not|never|no|without|avoid|dont)\s+(\w+\s+){0,3}?(commit|push)\w*", re.I)

_FORCE = _LazyRe(r"\b(force[\s-]*push|push\s+--force|--force-with-lease|zorla\s*push|force\s+ile)(?!\s*(yapma|etme|atma|kullanma)|\w*\s+yapma)", re.I)
_AMEND = _LazyRe(r"(--amend|\bamend\w*)\b", re.I)
_BRANCH = _LazyRe(
    r"\b(branch|dal)\w*\s+(aç|oluştur|yarat|create|değiştir|switch)(?!m[ae])"
    r"|\b\S+'?(e|a|ye|ya)\s+(geç|switch)\b(?!m[ae])"
    r"|\b(switch|checkout)\s+(to\s+)?\S+"
    r"|\b(yeni|new|create|switch\s+to|checkout)\s+(a\s+)?(branch|dal)\b"
    r"|checkout\s+-b|switch\s+-c|\bbranch'?(e|a)\s+geç", re.I)
_PR = _LazyRe(r"\b(pr|pull\s*request|merge\s*request)\b\s*(aç|oluştur|create|open|gönder)(?!m[ae])"
                 r"|\b(open|create)\s+(a\s+)?(pr|pull\s*request)\b", re.I)
_GH_WRITE = _LazyRe(r"\b(issue|release|label|gist)\w*\s+(aç|oluştur|kapat|yaz|ekle|create|close|open|publish|yayınla)(?!m[ae])"
                       r"|\b(pr|issue)\w*\s+(yorum|comment)\w*\s+(yaz|ekle|bırak|post|add)", re.I)
_SYNC = _LazyRe(r"\b(merge|rebase|pull|cherry-pick|revert)\w*\s+(et|yap|çek|it|this|that|from|origin|main|master|develop|the)(?!m[ae])"
                   r"|\b(son\s+)?değişiklikleri\s+çek(?!m[ae])"
                   r"|\bgit\s+(merge|rebase|pull|cherry-pick|revert)\b|\b(pull|çek)\s+(the\s+)?(latest|son\s+değişiklikleri)", re.I)
_TAG = _LazyRe(r"\b(tag|etiket)\w*\s+(oluştur|at|ekle|create|bas)(?!m[ae])|\bcreate\s+(a\s+)?tag\b|\bgit\s+tag\s+\S|\btag'?l[ae]\w*", re.I)
_STASH = _LazyRe(r"\bstash\w*\s*(le|yap|et|it|this|changes|pop|apply)?(?!m[ae])|\bgit\s+stash\b", re.I)
_HARD_RESET = _LazyRe(r"reset\s+--hard|hard\s+reset|git\s+clean|untracked\s+(dosyaları|files)\s*(sil|temizle|remove|delete)", re.I)
_DISCARD = _LazyRe(r"\b(değişiklikleri\s+(at|sil|geri\s*al)(?!m[ae])|discard\s+(the\s+)?changes|reset\s+--hard)\b", re.I)


def _lower(text: str) -> str:
    """Lower-cased user text with typographic apostrophes (iOS/macOS smart punctuation) read as the ASCII one."""
    return norm_lower(text or "").replace("’", "'").replace("‘", "'")


def git_grants(prompt: str) -> List[str]:
    text = _lower(prompt)
    cats: Set[str] = set()
    if text.strip().startswith(("/orkestra:ship", "/orkestra-ship")):         # Claude Code / Cursor skill names
        cats |= {"commit", "push"}
    if text.strip().startswith(("/orkestra:rollback", "/orkestra-rollback")):
        cats.add("discard")

    commit_ok = bool(_COMMIT_POS.search(text)) and not _COMMIT_NEG.search(text)
    push_neg = [m for m in _PUSH_NEG.finditer(text) if not re.search(r"force[\s-]*$", text[:m.start()])]
    push_ok = bool(_PUSH_POS.search(text)) and not push_neg
    if _QUESTION_TAIL.search(text):
        q = _QUESTION_TAIL.search(text).group(0).lower()  # type: ignore[union-attr]
        if q.startswith(("commit", "komit")):
            commit_ok = False
        else:
            push_ok = False
    if re.search(r"\bpush\s+(bildirim|notification|servis|service|server|sunucu|token|izni|permission)", text):
        push_ok = bool(re.search(r"\bgit\s+push\b|\bpush'?la(?!n?m[ae])\w*\s*(ve|and)?\s*$", text))
    if _PLURAL.search(text) and not re.search(r"\bcommit'?le(yip|yin|\b|sene)|\bcommit'?ler\s*m[ıi]s[ıi]n", text):
        commit_ok = False
    for m in _NEG_EN.finditer(text):
        verb = m.group(3).lower()
        if verb == "commit":
            commit_ok = False
        elif verb == "push":
            push_ok = False

    if commit_ok:
        cats.add("commit")
    if push_ok:
        cats.add("push")
        if _FORCE.search(text):
            cats.add("force")
    if _AMEND.search(text) and (commit_ok or re.search(r"\bamend\w*\s+(the\s+)?(last\s+)?commit|son\s+commit'?i\s+amend", text)):
        cats |= {"amend", "commit"}
    if _STASH.search(text):
        cats.add("stash")
    if _HARD_RESET.search(text):
        cats.add("hard_reset")
    if _BRANCH.search(text):
        cats.add("branch")
    if _PR.search(text):
        cats.add("pr")
    if _GH_WRITE.search(text):
        cats.add("gh_write")
    if _DISCARD.search(text):
        cats.add("discard")
    if _SYNC.search(text):
        cats.add("sync")
    if _TAG.search(text):
        cats.add("tag")
    return sorted(cats)


# --- control words: what the USER says to let a chat that does not drive a run change it (guards.ownership_guard)
# and to let go of the run the chat is bound to. A mention is not a request: "add a pause button to the player" and
# "the abort handler is broken" say nothing about the run, so the verb must have the run (or "it", "this") for its
# object and end its clause: "pause the run", "pause this and fix X", "duraklat", "bu işi durdur".
_CTL_START = r"(?:^|[.!?;:,\n]\s*|\b(?:please|pls|and|then|now|just|ok|okay|hey|lütfen|şimdi|hemen|ve|sonra)\s+)"
_CTL_OBJ_EN = (r"(?:(?:the|this|that|my|our|current|these|all)\s+)?"
               r"(?:runs?|orkestra|tasks?|jobs?|work|workflow|pipeline|everything|session|chat|it|this|that|here|now)")
_CTL_END_EN = r"(?=\s*(?:$|[.!?;:,\n]|\b(?:please|pls|now|and|then|thanks|for\s+now)\b))"
_CTL_OBJ_TR = (r"(?:(?:bu|şu|o|tüm|bütün)\s+)?"
               r"(?:run|orkestra|iş|görev|çalışma|süreç|oturum|sohbet|her\s*şey|hepsi|bun|şun|on)[a-zçğıöşü]{0,5}(?:'[a-zçğıöşü]+)?")
_CTL_END_TR = r"(?=\s*(?:$|[.!?;:,\n]|\b(?:lütfen|şimdi|hemen|ve|sonra|çünkü)\b))"
_PAUSE_WORD = _LazyRe(
    _CTL_START + r"(?:pause|stop|halt|hold\s+off)\s+" + _CTL_OBJ_EN + _CTL_END_EN
    + r"|^\s*(?:please\s+|pls\s+)?(?:pause|halt)\b[\s.!]*$"
    + r"|" + _CTL_START + r"(?:" + _CTL_OBJ_TR + r"\s+)?(?:duraklat|durdur|askıya\s+al|beklet)[a-zçğıöşü]*" + _CTL_END_TR,
    re.I)
_ABORT_WORD = _LazyRe(
    _CTL_START + r"(?:abort|cancel|kill|terminate|end)\s+" + _CTL_OBJ_EN + _CTL_END_EN
    + r"|^\s*(?:please\s+|pls\s+)?abort\b[\s.!]*$"
    + r"|" + _CTL_START + r"(?:" + _CTL_OBJ_TR + r"\s+)?(?:iptal\s+et|iptal\s+edelim|vazgeç(?:tim|tik|iyorum)|sonlandır)"
    + r"[a-zçğıöşü]*" + _CTL_END_TR,
    re.I)
# anchored like pause / abort: the verb opens its clause and ends it (optionally with the run for object), so "do not
# resume this run", "I will continue tomorrow" and "resolved conflicts in README" are no request
_RESUME_START = r"(?:" + _CTL_START + r"|\b(?:yes|yeah|yep|sure|evet|tamam|olur)\s+)"
_RESUME_WORD = _LazyRe(
    _RESUME_START + r"(?:resume|continue|go\s+on|carry\s+on|keep\s+going|proceed|unblock\w*)(?:\s+" + _CTL_OBJ_EN + r")?" + _CTL_END_EN
    + r"|" + _RESUME_START + r"(?:resolved|unblocked)" + _CTL_END_EN
    + r"|" + _RESUME_START + r"(?:devam(?:\s+(?:et|edelim|edin|ediyoruz))?|çözüldü)" + _CTL_END_TR
    + r"|kald[ıi][gğ][ıi]n\s+yerden(?!\s+devam\s+etme)|/orkestra[-:]resume\b"
    # the bare yes to "shall I continue the run?"
    + r"|^\s*(?:yes|yeah|yep|sure|ok|okay|go\s+ahead|please\s+do|evet|tamam|olur|başla)[\s.!]*$", re.I)
# the user switches topic without naming the other project
_SWITCH_WORD = _LazyRe(
    r"\b(?:forget|drop|leave|ditch|abandon)\s+(?:about\s+)?(?:this|that|it|the\s+(?:run|task|job|work|feature))\b|"
    r"\bnever\s*mind\b|\b(?:new|different|another|unrelated)\s+(?:topic|subject)\b|\bunrelated\s+(?:question|request|task)\b|"
    r"\b(?:change|switch)\s+(?:of\s+)?(?:topic|subject)\b|\blet'?s\s+(?:do|talk\s+about)\s+something\s+else\b|"
    r"\bbun[ua]\s+(?:bırak|unut|boşver)\w*|\bkonu\s+değiş\w*|\bbaşka\s+(?:bir\s+)?(?:konu|proje)\w*|"
    r"\bfarklı\s+bir\s+(?:konu|proje)\w*|\bboşver\b", re.I)


def control_grants(prompt: str) -> List[str]:
    """Which run-control commands this prompt asks for: pause, abort, resume (`resume` also covers unblock, and the
    plain words that tell a chat to carry on). A hand-back or notification is not the user and never reaches this."""
    text = _lower(prompt)
    cats: Set[str] = set()
    if _PAUSE_WORD.search(text):
        cats.add("pause")
    if _ABORT_WORD.search(text):
        cats.add("abort")
    if _RESUME_WORD.search(text):
        cats.add("resume")
    return sorted(cats)


def prompt_grants(prompt: str) -> List[str]:
    """Everything the user's message permits: the git grants and the run-control grants (stored together per prompt)."""
    return sorted(set(git_grants(prompt)) | set(control_grants(prompt)))


_APPROVE_EXACT = _LazyRe(
    r"^(onay|onayla|onaylıyorum|onayladım|onaylandı|onay\s+verdim|onay\s+veriyorum|evet|evet onay|evet onaylıyorum|"
    r"evet başla|tamam başla|tamam onay|başla|devam|devam et|uygula|kabul|kabul ediyorum|lgtm|approve|approved|"
    r"i approve|go ahead|yes|ok başla|okey başla|"
    # English forms of the same short answers
    r"ok|okay|ok(?:ay)?,?\s+(?:start|go|go\s+ahead|proceed)|yes,?\s+(?:please|go\s+ahead|proceed|start|continue|approve)|"
    r"proceed|continue|start|begin|accept|accepted|do\s+it|go\s+for\s+it|looks\s+good|looks\s+good\s+to\s+me|"
    r"sounds\s+good|ship\s+it|approve\s+it|approve\s+the\s+plan)"
    r"[\s.!👍✅]*$", re.I)
# English verbs that turn a sentence into a change request (also read on the free text after an option label).
_EN_VERB = (r"(?:change|add|remove|drop|skip|rename|replace|swap|delete|exclude|include|modify|revise|rework|rewrite|"
            r"redo|split|reorder|tweak|adjust|edit|simplify|reduce|limit|restrict|extend|expand)")
_NEG_HINT = _LazyRe(r"(\?|\bmı\b|\bmi\b|\bmu\b|\bmü\b|değil|yok\b|bekle|vermiyorum|etmiyorum|\w+m[ıi]yorum\b|"
                       r"\bama\b|\bfakat\b|\bancak\b|\bbut\b|\bnot\b|\bno\b|\bwait\b|hayır|iptal|vazgeç|değiştir|"
                       r"revize|düzelt|ekle|çıkar|reject|cancel|change|later|sonra|yarın|"
                       r"\b" + _EN_VERB + r"\b|\b(?:except|without|instead|only|first|also|however|although)\b|n't)", re.I)

# Sentence-level approval: user affirms the plan and may add execution fluff ("yap", "en uzman…").
_APPROVE_PHRASE = _LazyRe(
    r"(?:^|[\s,;:.!])("
    r"onay\s*(?:lıyorum|ladım|landı|lıyoruz)|"
    r"onay\s+ver(?:dim|iyorum|iyoruz|ildi)|"
    r"(?:bu\s+)?plan(?:ı|i)?(?:\s+da)?\s+onay\w*|"
    r"onaylıyorum|onayladım|"
    r"i\s+approve|we\s+approve|\bapproved\b|\blgtm\b|go\s+ahead"
    r")(?:\b|$)",
    re.I,
)
# The bare imperative ("approve it and go") counts only as the opening words of the message: anywhere else "approve it"
# is a question, a third person ("only the lead can approve this") or another object ("approve this pull request").
_APPROVE_IMPERATIVE = _LazyRe(
    r"^approve\s+(?:it|this|that|the\s+plan|the\s+spec)(?=\s*(?:$|[.!,;:]|\b(?:and|then|now|please|pls|thanks|thank|"
    r"go|start|begin|implement|proceed|continue|already)\b))",
    re.I,
)
# Change/condition requests that void an otherwise-approving sentence.
_APPROVE_CHANGE = _LazyRe(
    r"(?:\bama\b|\bfakat\b|\bancak\b|\bbut\b).{0,60}?(?:değiştir|çıkar|ekle|revize|düzelt|T0\d{2}|task)|"
    # English: a contrast followed by a change cue, an exception or substitution, an imperative change verb
    # ("approve it, skip T002"), or a change verb used as a request ("please change ...", "can you add ...")
    r"\b(?:but|however|although|though|yet)\b.{0,80}?(?:\b" + _EN_VERB + r"\b|\b(?:instead|first|only|also|not|no|wait|hold)\b|"
    r"n't|\bT0\d{2}\b|\btasks?\b)|"
    r"\b(?:except|unless|instead|apart\s+from|other\s+than|aside\s+from)\b|"
    # a condition, a limit or a retraction anywhere in the message ("I approve, provided ...", "for T001 only",
    # "the first two tasks", "I approve without T003", "actually, wait", "do not implement T003")
    r"\b(?:provided|providing|assuming|as\s+long\s+as|so\s+long\s+as|pending|subject\s+to|contingent|conditional(?:ly)?|"
    r"on\s+(?:the\s+)?condition|with\s+the\s+condition|given\s+that|once|until|only)\b|"
    r"\b(?:first|last)\s+(?:one|two|three|four|five|six|seven|eight|nine|ten|\d+)\b|"
    r"\b(?:without|excluding|minus|skipping|besides)\s+(?:the\s+)?(?:task\s+)?T0\d{2}\b|\bT0\d{2}\b|"
    r"\b(?:actually|not\s+yet|second\s+thoughts?|never\s*mind|scratch\s+that|hold\s+(?:on|off))\b|"
    r"(?:^|[.;!?]\s*)(?:wait|hold|stop)\b|"
    r"(?:^|[.;!?:,]\s*)(?:(?:and|also|please|pls)\s+)*(?:do\s*n'?t|do\s+not|never)\s+"
    r"(?:implement|touch|change|build|include|deploy|ship|start|run|commit|push|modify|edit|remove|delete|add|use|skip|"
    r"do|write|create|begin|proceed)\b|"
    r"(?:^|[.;!?:,]\s*)(?:(?:and|also|please|pls)\s+)*" + _EN_VERB + r"\b|"
    r"\b(?:please|pls|also|plus|then|first|let'?s|(?:can|could|would|will)\s+you|"
    r"(?:i|we)\s+(?:want|need|would\s+like)(?:\s+you)?\s+to|you\s+(?:should|must|need\s+to))\s+(?:also\s+)?"
    + _EN_VERB + r"\b|"
    # a negation right in front of the verb voids the approval; a negation elsewhere ("don't worry, I approve") does not
    r"\b(?:not|never|don'?t|cannot|can'?t|won'?t|wouldn'?t|shouldn'?t|couldn'?t|isn'?t|aren'?t|haven'?t|hasn'?t|"
    r"didn'?t|doesn'?t)\s+(?:\w+\s+){0,3}?approv|"
    r"\b(?:to|let\s+me|should|shall|could|would|might|may|if|unless|until|before|when)\s+(?:i\s+|we\s+|you\s+)?approv|"
    # a promise or a condition instead of a decision ("I'll approve it tomorrow", "approve it once the tests pass")
    r"\b(?:i'?ll|i\s+will|we'?ll|we\s+will|i'?m\s+going\s+to|i\s+am\s+going\s+to|gonna|will)\s+(?:\w+\s+)?approv|"
    r"\bapprov\w*\s+(?:(?:it|this|that|them|the\s+plan|the\s+spec|plan|spec)\s+)?"
    r"(?:tomorrow|tonight|later|soon|after|once|when|if|until|before|as\s+soon|conditionally|provided|assuming)\b|"
    r"onay\s*vermiyorum|onaylamıyorum|reddet\w*|"
    # Turkish: a negated, modal, future or conditional form of "approve", a contrast, an exception or a limit
    r"\bonayl(?:am|anm|ayab|ayac|ayay|ars|asa|ayınca|adıys|ayana|adıkt)\w*|"
    r"\b(?:ama|fakat|ancak|lakin)\b|\b(?:hariç|haricinde|dışında|yalnızca|yalnız|sadece|şartıyla|koşuluyla)\b|"
    r"\b(?:başlama|yapma|etme)(?:yın)?\b|"
    r"\b(?:değiştir|revize|iptal|vazgeç|reject|cancel)\w*",
    re.I,
)
# "start with T001" names where to begin; any other task reference in an approving message limits what is approved.
_START_WITH_TASK = _LazyRe(
    r"\b(?:start|begin|starting|beginning|go|proceed|continue)\s+(?:with|from|at|on|by)\s+T0\d{2}\b|"
    r"\bT0\d{2}(?:'?(?:den|dan|ten|tan))?(?:\s+ile)?\s+(?:başla|başlayın|başlayalım|devam)\w*", re.I)


def _change_requested(text: str) -> bool:
    return bool(_APPROVE_CHANGE.search(_START_WITH_TASK.sub(" ", text)))


_APPROVE_QUESTION = _LazyRe(
    r"\bonay\w*\s+(?:mı|mi|mu|mü)\b|\bonaylıyor\s+musun|\bonaylar\s+mısın|\?\s*$",
    re.I,
)

_EXPLICIT_REJECT = _LazyRe(r"(onaylamıyorum|onay\s+vermiyorum|reddet\w*|\bhayır\b|\biptal\b|vazgeç\w*|değiştir\w*|"
                              r"revize|düzelt\w*|\bçıkar\w*|\bekle(?!me)\w*\s|\breject\w*|\bcancel\w*|\bchange\w*|\bama\b|\bbut\b|"
                              r"\bexcept\b|\binstead\b|onaylamad[ıi][mk]|onaylanmad[ıi]|onaylamak\s+istemiyorum)", re.I)
_APPROVAL_NOUN = r"(?:approval|sign[- ]?off|ok|okay|confirmation|go[- ]ahead|consent)"
# Where an imperative to the agent may start: the message, a clause, or after "please" / "you must" / "I want you to".
_CLAUSE_START = (r"(?:^|[.!?;:,\n]\s*|\b(?:please|pls|and|then|also|just|always|first|"
                 r"you\s+(?:must|should|need\s+to|have\s+to|will)|(?:i|we)\s+(?:want|need)\s+you\s+to|"
                 r"make\s+sure(?:\s+you)?|be\s+sure\s+to)\s+)")
# ... and where it ends: "review the plan first" is a request, "see the plan for the migration" is a sentence about the work.
_CLAUSE_END = r"(?=\s*(?:$|[.!?;:,\n]|\b(?:first|before|prior|beforehand|until)\b))"
_MANUAL_APPROVAL_REQUEST = _LazyRe(
    r"\b(?:ben\s+karar\s+verici(?:\s+ol(?:ayım|malıyım|mak\s+istiyorum))?|"
    r"karar(?:ı|ları)\s+ben\s+ver(?:eyim|meliyim|mek\s+istiyorum)|"
    r"(?:onayı|onayları|onay)\s+bana\s+sor\w*|bana\s+onay\s+sor\w*|benden\s+onay\s+al\w*|"
    r"i\s+want\s+to\s+(?:decide|approve|be\s+the\s+decision\s+maker)|"
    r"let\s+me\s+(?:decide|approve)|"
    r"ask\s+me\s+for\s+(?:my\s+)?" + _APPROVAL_NOUN + r"|"
    r"ask\s+me\s+before\s+(?:you\s+)?(?:start|begin|implement|code|coding|build|proceed|writ)\w*|"
    # "the plan needs my approval" is about the agent's plan; "releases need my approval" is a feature request
    r"(?:plan|spec)\s+(?:needs?|requires?)\s+my\s+(?:own\s+)?" + _APPROVAL_NOUN + r")\b",
    re.I,
)
# The wordings that are also ordinary feature requests ("a page where releases need my approval", "see the design in dark
# mode", "enable manual approval of refunds") count only as a short imperative that ends its clause.
_MANUAL_APPROVAL_IMPERATIVE = _LazyRe(
    _CLAUSE_START + r"(?:"
    r"(?:wait\s+for|require|get)\s+my\s+(?:own\s+)?" + _APPROVAL_NOUN + r"|"
    r"(?:i\s+want|i'?d\s+like|i\s+would\s+like)\s+to\s+(?:review|check|read|see|look\s+at)\s+(?:the|this|your|that)\s+"
    r"(?:plan|spec)(?:\s+(?:and|or|&)\s+(?:the\s+)?(?:plan|spec))?|"
    r"let\s+me\s+(?:review|check|read|see|look\s+at)\s+(?:the|this|your|that)\s+(?:plan|spec)|"
    r"(?:use|enable|switch\s+to|turn\s+on|set)\s+(?:the\s+)?manual\s+(?:plan[- ])?approval(?:\s+mode)?"
    r"(?:\s+for\s+(?:this|the)\s+(?:run|plan|task|job))?(?:\s+(?:please|pls))?"
    r")\b" + _CLAUSE_END,
    re.I,
)
# A negation cancels the request only when it is about asking / approving in the same sentence ("don't ask me", "go
# without my approval", "no need for my approval"): "without breaking the API. Ask me for approval" does not.
_SAME_SENTENCE = r"(?:[^.!?;\n]|\.(?=\S))"
_MANUAL_APPROVAL_NEGATION = _LazyRe(
    r"\b(?:don'?t|do\s+not|never|no\s+need\s+to)\b" + _SAME_SENTENCE + r"{0,60}\b(?:ask|approval|approve|decide)\w*|"
    r"\b(?:no\s+need\s+for|without)\s+(?:(?!(?:and|but|then|so|or|also)\b)[\w'-]+\s+){0,3}(?:ask|approval|approve|decide)\w*|"
    r"\bnot\s+(?:the\s+)?manual\s+(?:plan[- ])?(?:approval|mode)\b|"
    r"\b(?:sorma|sormana\s+gerek\s+yok|onay\s+bekleme|onay\s+isteme)\b|"
    r"\b(?:unless|if|when|whenever|only\s+if|eğer|şayet|kullanıcı\s+isterse|"
    r"user\s+(?:wants|asks|says))\b.{0,100}\b(?:ben|bana|me|let\s+me|karar|onay|approve|i\s+want|i'?d\s+like|"
    r"i\s+would\s+like)\b",
    re.I,
)


def manual_approval_requested(prompt: str) -> bool:
    """Whether the user clearly asks to retain the plan-approval decision."""
    text = _lower(prompt)
    asked = _MANUAL_APPROVAL_REQUEST.search(text) or _MANUAL_APPROVAL_IMPERATIVE.search(text)
    return bool(asked) and not _MANUAL_APPROVAL_NEGATION.search(text)


def approval(prompt: str) -> Optional[str]:
    """Unambiguous plan approval → 'approve'; explicit change/reject → 'reject'; else None.

    Whole-message short forms still work (`onay`, `onaylıyorum`). Longer messages that clearly
    approve the plan and only add execution instructions (`…onaylıyorum yap`, `onay verdim devam et`)
    also count as approve — unless they attach a change request (`ama T001'i çıkar`).
    """
    text = _lower(prompt).strip()
    if not text:
        return None
    if _APPROVE_EXACT.match(text) and not _NEG_HINT.search(text):
        return "approve"
    approving = _APPROVE_PHRASE.search(text) or _APPROVE_IMPERATIVE.match(text)
    if approving and not _change_requested(text) and not _APPROVE_QUESTION.search(text):
        return "approve"
    if _EXPLICIT_REJECT.search(text + " "):
        return "reject"
    return None


def _option_keys(kind: str, index: int) -> frozenset:
    """Folded spellings (every language) of one option of an AskUserQuestion flow (messages/protocol.PROMPTS)."""
    pair = _protocol.PROMPTS[kind]["options"][index]                        # type: ignore[index]
    return frozenset(_protocol.fold(label) for label in pair)


_APPROVE_LABELS = _option_keys("approve", 0)        # "Approve and start implementing" / "Onayla — uygulamaya başla"
_ROLLBACK_APPLY, _ROLLBACK_ALL, _ROLLBACK_CANCEL = (_option_keys("rollback", i) for i in range(3))
# the Cursor chat form of the rollback answer: `/orkestra-rollback apply [--all]` (Turkish: `uygula`)
_ROLLBACK_COMMAND = _LazyRe(r"^orkestra rollback (apply|uygula)( all)?$")


def approval_answer(answer: str) -> Optional[str]:
    """AskUserQuestion answer: the predefined option label, or free text typed via 'Other'.

    The approve option is accepted in either language, without regard to case, diacritics or the dash
    (`Onayla — uygulamaya başla`, `onayla uygulamaya basla`, `APPROVE AND START IMPLEMENTING`); text after the label
    still approves unless it carries a change request. Every other option (`I want changes`, `Cancel`, `Değişiklik
    istiyorum`, `İptal et`) and anything unrecognised is a reject."""
    a = _lower(answer).strip()
    if not a:
        return None
    key = _protocol.fold(a)
    if key in _APPROVE_LABELS or key in ("onayla", "approve"):
        return "approve"
    if (any(key.startswith(label + " ") for label in _APPROVE_LABELS)
            and not _NEG_HINT.search(a) and not _change_requested(a)):
        return "approve"
    if _APPROVE_EXACT.match(a) and not _NEG_HINT.search(a):
        return "approve"
    return "reject"


def rollback_answer(answer: str) -> Optional[str]:
    """Answer of the rollback flow: `apply` | `apply_all` | `cancel`, or None when it is none of them.

    Accepts the option labels of either language (`Apply (orkestra changes only)`, `Uygula (yalnız orkestra
    değişiklikleri)`, ...) and the Cursor chat command `/orkestra-rollback apply [--all]` (`uygula`). Exact matches
    only: a near miss is None, never a guess, because applying a rollback reverts files."""
    key = _protocol.fold(answer)
    if key in _ROLLBACK_APPLY:
        return "apply"
    if key in _ROLLBACK_ALL:
        return "apply_all"
    if key in _ROLLBACK_CANCEL:
        return "cancel"
    m = _ROLLBACK_COMMAND.match(key)
    if m:
        return "apply_all" if m.group(2) else "apply"
    return None


# --- run size (XS/S finish fast; M/L keep the full pipeline) -----------------

SIZES = ("XS", "S", "M", "L")

_EXPLICIT_SIZE = _LazyRe(
    r"(?:(?:--)?size|boyut)\s*[:=]?\s*(XS|S|M|L)\b|\b(XS|S|M|L)\s*(?:boyut|size)\b",
    re.I,
)
_L_SIZE = _LazyRe(
    r"\b(migrat(?:e|ion|ing)?|bulk|çoklu\s+platform|multi[- ]?platform|"
    r"tüm\s+(sistem|uygulama|proje|kod|modül)|rewrite\s+the\s+whole|"
    r"büyük\s+(iş|özellik|refactor|değişiklik)|end[- ]to[- ]end|"
    r"across\s+(all|every)|her\s+(platform|ekran|modül)|"
    r"full\s+rewrite|kapsamlı\s+refactor)\b",
    re.I,
)
_RISK_FLOOR_M = _LazyRe(
    r"\b(auth(?:entication|orization)?|oauth|payment|billing|stripe|revenuecat|"
    r"deploy|store\s+submit|testflight|play\s+console|schema\s+change|"
    r"security|kvkk|gdpr|credential|secret|encryption|rls|"
    r"permission|pci|hipaa|"
    r"odeme|ödeme|giris\s+(yap|ekle|ekran|sistem)|giriş\s+(yap|ekle|ekran|sistem)|"
    r"sifre\w*|şifre\w*|parola\w*|yetki\w*|fatura\w*|abonelik\w*|"
    r"sema|şema|kimlik|webhook|password|"
    r"drop\s+table|drop\s+column|truncate|"
    r"login|session|(?:auth|access|refresh|api|bearer|id)\s+token|jwt|sso|injection|xss|csrf|checkout|race\s+condition|concurrency|deadlock|"
    r"rate\s+limit|crypto|data\s+loss|delete\s+account|cron|dependency\s+upgrade)\b|"
    # an index is a risk only in a database context ("index.html", "the index page" are not)
    r"\b(?:(?:create|add|drop|rebuild)\s+(?:an?\s+)?(?:unique\s+|composite\s+)?index\s+on|"
    r"(?:db|sql|database|table|migration)s?\s+index\w*|index\s+(?:on|for)\s+(?:the\s+)?(?:\w+\s+)?table)\b",
    re.I,
)
# A real change, not a question and not a typo fix. "yazım" does not match "yaz".
_CHANGE = _LazyRe(
    r"\b(add|ekle|implement|migrate|deploy|delete|remove|kaldir|kaldır|sil|"
    r"olustur|oluştur|kur|bagla|bağla|enable|disable)\b",
    re.I,
)
_RANK = {"XS": 0, "S": 1, "M": 2, "L": 3}
_XS_SIZE = _LazyRe(
    r"\b(typo|yazım\s*hatası|rename|yeniden\s*adlandır|one[- ]line|tek\s*satır|"
    r"trivial|çok\s*basit|çok\s*küçük|minik|nit\b|cosmetic|"
    r"fix\s+the\s+typo|sadece\s+(şunu|bunu)\s+değiştir|"
    r"tek\s+kelime|one\s+word)\b",
    re.I,
)
_S_SIZE = _LazyRe(
    r"\b(küçük|basit|small\s+(fix|change|bug|tweak)|bug\s*fix|hotfix|"
    r"bir\s+dosya|tek\s+dosya|few\s+files|birkaç\s+dosya|"
    r"add\s+(a\s+)?(test|helper|function|method)|"
    r"(fonksiyon|function|metod|method)\s+ekle|"
    r"sub\s+fonksiyonu|düzelt(?:me)?\b|fix\s+(this|the|a)\b)\b",
    re.I,
)
# Small, local edits (a colour, a title, a version, a log line). Checked after risk and before the generic M cue, so
# "add logging to the worker" is not read as "add X to Y feature". Vocabulary only grows.
_S_SMALL = _LazyRe(
    r"\b(?:(?:change|update|set|make|fix)\s+(?:the\s+)?(?:\w+\s+){0,5}"
    r"(?:colou?r|title|label|heading|header|footer|padding|margin|font|icon|sticky|wording|copy)\b|"
    r"bump\s+(?:the\s+)?version|remove\s+(?:an?\s+|the\s+)?unused|sort\w*\s+(?:\w+\s+){0,3}alphabetical\w*|"
    r"add\s+(?:some\s+)?(?:logging|logs?|comments?|docstrings?)\b|"
    r"\w*(?:renk|reng|başlık|başlığ|baslik|metin|yazı|simge)\w*\s+(?:\w+\s+){0,2}?(?:değiştir|degistir|güncelle|guncelle)\w*|"
    r"readme\S*\s+(?:\w+\s+){0,3}not\w*\s+ekle)",
    re.I,
)
_M_SIZE = _LazyRe(
    r"\b(feature|özellik|ekran|screen|endpoint|api\s+ekle|yeni\s+(modül|ekran|sayfa)|"
    r"refactor\b|yeniden\s+yapılandır|dark\s+mode|yeni\s+özellik|"
    r"(?:add|export|import|implement)\s+(?!(?:a\s+)?(?:test|helper|function|method)\b)(?:\w+\s+){0,3}(?:to|in|into)\s+(?:the\s+)?\w+)\b",
    re.I,
)
# Explain / Q&A / read-only asks: still open a tracked run (dashboard), but stay on the XS fast track.
_ADVISORY = _LazyRe(
    r"(?:"
    r"\b(?:explain|describe|summarize|clarify|walk\s+me\s+through|help\s+me\s+understand)\b|"
    r"\b(?:what|how|why|where|when|which)\b.{0,40}\b(?:does|is|are|do|did|work|mean|happen)\b|"
    r"\b(?:açıkla|anlat|özetle|nedir|nasıl|neden|niçin|nerede|ne\s+zaman|hangisi)\b|"
    r"\b(?:ne\s+yapıyor|ne\s+işe\s+yarar|nasıl\s+çalışır|kısaca\s+anlat)\b|"
    r"\b(?:read[- ]only|no\s+code\s+change|kod\s+değiştirme|sadece\s+oku|yalnız\s+oku)\b"
    r")",
    re.I,
)


def _pure_question(text: str, low: str) -> bool:
    """Explain or Q&A with no change verb. A risky noun in the question does not make it a build."""
    return bool(_ADVISORY.search(text)) and not _CHANGE.search(low)


def is_advisory_request(request: str) -> bool:
    """True when the ask is explain/Q&A/read-only: track on the dashboard, do not invent a product change."""
    text = (request or "").strip()
    if not text:
        return False
    low = norm_lower(text)
    if _L_SIZE.search(low) or _CHANGE.search(low):
        return False
    return _pure_question(text, low)


def size_floor(request: str) -> str:
    """Smallest size this request may use. A risky change cannot sit below M. A question or a typo can stay XS."""
    text = (request or "").strip()
    if not text:
        return "XS"
    low = norm_lower(text)
    if _pure_question(text, low):
        return "XS"
    if _L_SIZE.search(low):
        return "L"
    if _RISK_FLOOR_M.search(low) and not _XS_SIZE.search(low):
        return "M"
    return "XS"


def clamp_size(size: str, request: str) -> str:
    """Raise a chosen size up to the floor. Never lower it."""
    floor = size_floor(request)
    if _RANK.get(size, _RANK["M"]) < _RANK[floor]:
        return floor
    return size if size in _RANK else "M"


def resolve_size(requested: str, request: str) -> str:
    """`--size` may go smaller on a safe request. It cannot undercut a risk, or inflate a typo or a question."""
    inferred = infer_size(request)
    if not requested or requested == "auto":
        return inferred
    chosen = clamp_size(requested, request)
    # Only a request inferred XS (a typo or a question) is protected from growth; a short feature may be sized up.
    if inferred == "XS" and _RANK[chosen] > _RANK[inferred] and size_floor(request) == "XS":
        return inferred
    return chosen


def title_from_request(request: str, max_words: int = 6) -> str:
    """Short title for `orkestra start` / pre-opened runs (≤6 words; ASCII-safe via slugify later)."""
    words = re.findall(r"[^\W_]+(?:['’-][^\W_]+)*", (request or "").strip(), flags=re.UNICODE)
    if not words:
        return "request"
    return " ".join(words[: max(1, int(max_words))])


def skips_plan_critic(size: str) -> bool:
    """XS/S: no opus plan-critic round — plan check + final review still apply."""
    return (size or "") in ("XS", "S")


def skips_adversary(size: str) -> bool:
    """XS/S: final reviewer only; adversary is reserved for M/L."""
    return (size or "") in ("XS", "S")


def min_context_chars(size: str) -> int:
    """Discovery note floor: XS can be a short self-written note."""
    return 80 if (size or "") == "XS" else 200


def _base_size(text: str, low: str, n: int) -> str:
    if _pure_question(text, low) and n < 400:
        return "XS"
    if _L_SIZE.search(low):
        return "L"
    if _RISK_FLOOR_M.search(low) and not _XS_SIZE.search(low):
        return "M"
    if is_advisory_request(text) and n < 400:
        return "XS"
    if _XS_SIZE.search(low) and n < 220:
        return "XS"
    if _S_SMALL.search(low) and n < 220:
        return "S"
    if _M_SIZE.search(low):
        return "M"
    if _S_SIZE.search(low):
        return "S"
    # An article or other non-code job does not need scouts, a plan critic, or an adversary.
    from .workkind import classify
    if classify(text) in ("writing", "general"):
        return "S"
    return "M"


def infer_size(request: str) -> str:
    """Pick XS/S/M/L from the user's request text. Prefer smaller when clearly safe; floor at M on risk.

    Explicit `boyut: S` in the text wins only when it stays above the risk floor
    and does not inflate a typo or a question. A feature or a risk stays M/L.
    A long small-fix note, and writing or general work without those signals, stays on the fast track.
    Unclear text still stays M.
    """
    text = (request or "").strip()
    if not text:
        return "M"
    low = norm_lower(text)
    base = _base_size(text, low, len(low))
    m = _EXPLICIT_SIZE.search(text)
    if not m:
        return base
    chosen = clamp_size((m.group(1) or m.group(2) or "M").upper(), text)
    if _RANK[base] <= _RANK["S"] and _RANK[chosen] > _RANK[base] and size_floor(text) == "XS":
        return base
    if _RANK[chosen] < _RANK[base]:
        return base
    return chosen


_CONTINUE_INTENT = _LazyRe(
    r"(?:"
    r"\bdevam(?:\s*et\w*|\s*edelim)?\b|"
    r"\bcontinue\b|\bresume\b|\b(?:go\s+on|keep\s+going|carry\s+on|proceed)\b|"
    r"/orkestra[-:]?resume\b|"
    r"\b(?:sonraki|next)\s+(?:g[oö]rev|task|ad[iı]m|step)\b|"
    r"\borkestra\s+(?:next|status)\b|"
    r"\b(?:durum(?:\s+ne)?|status|ne\s+oluyor)\b|"
    r"\bkald[iı][gğ][iı]n\s+yerden\b|"
    r"\bT\d{3}\b"
    r")",
    re.I,
)
# In-run operational asks (tests/gates/git/skills) keep the bind; they are not a new feature request.
_RUN_OPS_INTENT = _LazyRe(
    r"(?:"
    r"\b(?:test(?:ler(?:i)?)?|tests|gate|kapı|build|lint|typecheck|verify|coverage)\b|"
    r"\b(?:çalıştır|koş(?:tur)?|run\s+(?:the\s+)?tests?|run\s+(?:the\s+)?gate)\b|"
    r"\b(?:commitle|pushla|commit\s+et|push\s+et)\b|"
    r"/orkestra[-:]?\w+|"
    r"^\s*(?:uygula|apply)\b"
    r")",
    re.I,
)


def _mentions_bound_project(text: str, project, run_id: str = "", title: str = "") -> bool:
    from pathlib import Path as _Path
    project = _Path(project)
    pname = project.name.lower()
    if pname and re.search(rf"(?<![a-z0-9_-]){re.escape(pname)}(?![a-z0-9_-])", text):
        return True
    rid = str(run_id or "").lower()
    if rid and re.search(rf"(?<![a-z0-9_-]){re.escape(rid)}(?![a-z0-9_-])", text):
        return True
    try:
        if str(project.resolve(strict=False)).lower() in text:
            return True
    except Exception:
        pass
    for w in re.findall(r"[a-z0-9_-]{4,}", norm_lower(title or "")):
        if w in ("feature", "implement", "fix", "bug", "with", "from", "that", "this", "have", "into"):
            continue
        if re.search(rf"(?<![a-z0-9_-]){re.escape(w)}(?![a-z0-9_-])", text):
            return True
    return False


def _mentions_other_workspace_project(text: str, project) -> bool:
    from pathlib import Path as _Path
    project = _Path(project)
    pname = project.name.lower()
    try:
        from . import paths as _paths
        root = _paths.workspace_root()
    except Exception:
        return False
    if not root.is_dir():
        return False
    for p in root.iterdir():
        if not p.is_dir() or p.name.startswith("."):
            continue
        n = p.name.lower()
        if n == pname or len(n) < 3:
            continue
        if re.search(rf"(?<![a-z0-9_-]){re.escape(n)}(?![a-z0-9_-])", text):
            return True
    return False


def off_topic_vs_project(prompt: str, project, run_id: str = "", title: str = "") -> bool:
    """True when the user's message tells this chat to let go of the bound run. Only EXPLICIT signals do:
      * the user says to pause / stop / abort the run, or to switch topic ("forget this", "never mind", "duraklat",
        "başka bir konu") -- in either language;
      * the message names another project of the workspace and nothing in it ties it to the bound run.
    An ordinary message ("make the header blue", "devam", "thanks") keeps the binding: the run guards keep protecting the
    run, and the Stop hook releases a turn that did not drive it (activity.turn_drove_run).

    Ties to the bound run (they win over a named other project): its name / id / path / title keywords, an explicit
    continue (devam/continue/resume/next task/T0xx), a git permission or run operation, plan approval wording.
    """
    from pathlib import Path as _Path
    project = _Path(project)
    text = norm_lower(prompt or "")
    if not text.strip():
        return False
    said = _lower(prompt)
    if _PAUSE_WORD.search(said) or _ABORT_WORD.search(said) or _SWITCH_WORD.search(said):
        return True
    if _mentions_bound_project(text, project, run_id, title):
        return False
    if _CONTINUE_INTENT.search(text):
        return False
    # Git permission / run-ops lines ("şimdi commit et", "testleri çalıştır") are about the
    # bound run -- unbinding would drop the grant and open a hole in the quiet path.
    if git_grants(prompt) or _RUN_OPS_INTENT.search(text):
        return False
    if approval(text) == "approve":
        return False
    if manual_approval_requested(prompt):
        return False
    if re.fullmatch(r"(hayır|iptal|reject|vazgeç\.?|no)", text.strip()):
        return False                                    # the bare answer of an open question
    return _mentions_other_workspace_project(text, project)
