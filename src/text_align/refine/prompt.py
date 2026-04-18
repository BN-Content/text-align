"""System prompt assembly and per-verse message formatting for refine-alignment.

Block architecture
------------------
BASE_BLOCK is always included.  Nine CONDITIONAL_BLOCKS are included only when
the corresponding phenomenon is detected in the verse batch by detect_phenomena().
Two forced co-inclusions apply regardless of direct detection:
  - PASSIVE  → also includes IMPERSONAL
  - HINA     → also includes INFINITIVE
"""

from text_align.burrito.source import Source
from text_align.migrate.models import MigrateTarget


# ---------------------------------------------------------------------------
# Prompt blocks
# ---------------------------------------------------------------------------

BASE_BLOCK = """\
You are refining word-level alignments between a Bible translation and its Greek source
text (SBLGNT).

## ALIGNMENT DIRECTION

Alignments map translation → source. Each record associates one or more target tokens
with one or more source tokens. The direction matters: you are asking what Greek word(s)
are behind each translation word, not the reverse.

## ALIGNMENT PHILOSOPHY

Alignments are generous. When a translation word exists because of Greek grammar — a
preposition implied by a noun's case, a pronoun implied by a verb's person and number,
an article implied by context — it belongs in an alignment record. The goal is to
account for as many tokens as the Greek justifies, not to restrict alignment to strict
lexical equivalents.

## TOKEN ROLES

Every token in a record is either primary or secondary. For each English word, ask:

**Why does this word exist in the translation?**

- **It translates a specific Greek lexeme** → it is **primary** in that lexeme's record.
- **It exists because of grammar implied by the primary Greek token** (morphological
  person/number, case, aspect, mood, definiteness) with no separate Greek word of its
  own → it is **secondary** in the same record as the primary it depends on.
- **It translates a different Greek token** → it belongs in a **separate record** for
  that token, not as secondary here.

Find the English word that translates the Greek lexeme — that is primary. Every other
word in the same record exists only because of grammar encoded in that Greek token; those
are secondary. If a word could be primary to a different Greek token in the verse, give
it its own record instead.

Common secondary cases:

- **Supplied subject pronoun** — ἦλθεν (3sg) → "he came":
  "came" primary; "he" secondary (person/number in verb ending)

- **Auxiliary verb** — ἐδίδασκεν (imperfect) → "was teaching":
  "teaching" primary; "was" secondary (aspect in morphology)

- **Infinitive marker** — λαβεῖν → "to take":
  "take" primary; "to" secondary (Greek infinitive has no separate marker)

- **Indefinite article** — ἄνθρωπος (anarthrous) → "a man":
  "man" primary; "a" secondary (definiteness encoded in anarthrous form)

- **Case-implied preposition** — θεοῦ → "of God":
  "God" primary; "of" secondary (genitive case of θεοῦ, no separate Greek token)
  Note: other words from different records (e.g. "children") are never secondary here.

- **Case-implied preposition with article** — τοῖς σάββασιν → "on the Sabbath":
  source=[τοῖς],     target=["the"]            — article → "the", primary 1:1
  source=[σάββασιν], target=["on", "Sabbath"]  — "Sabbath" primary; "on" secondary

**Structural constraints:**

Every record must have at least one primary token on each populated side. A token cannot
be the only token on its side and also be marked secondary.

Each target token ID must appear in exactly one record per verse. Do not assign the same
target token to two records, even as secondary in one and primary in another.

## NEQ (NON-EQUIVALENT)

For each token without an obvious alignment, ask:

**Am I certain this token has no correspondent anywhere in the translation of this verse?**

- **YES, certain** → NEQ: a record with the token in one array and the other array
  empty, plus `meta.rel: "NEQ"`.
- **Uncertain / cannot determine** → leave the token **unrecorded**. Do not use NEQ
  as a fallback for difficulty or uncertainty.

Unrecorded tokens are normal — they mean the correspondence was not determined.
NEQ tokens make a positive claim that no correspondence exists. Using NEQ when you
are merely unsure corrupts the data.

NEQ records must not include meta.secondary. A token is either non-equivalent (NEQ)
or has a primary/secondary relationship with another token — never both.

## SURFACE FORM DIFFERENCES

Morphological differences between source and target — tense, voice, number, aspect —
do not prevent alignment. A Greek present indicative rendered as a past tense, or an
active rendered as a passive, may still be a valid alignment. The question is whether
lexical and semantic correspondence exists, not whether the surface forms match.

## CANDIDATES

The alignment candidates provided are initial word-level suggestions from automated
tools. They contain no secondary classification, no idiom flags, and some will be
wrong. Restructure, split, merge, or discard them freely. Use them as a rough starting
point, not as a framework to preserve.

## GLOSS FIELDS

Source token lines may include two gloss fields:

- **gloss**: contextual meaning, including grammatical role words the English requires
  (prepositions, articles, case markers). Example: "of [the] genealogy".
- **lexeme**: the bare lexeme meaning, stripped of role words. Example: "genealogy".
  For Hebrew tokens, the lexeme may include morphologically implied words separated by
  spaces (e.g. "he created" for a verb whose subject is encoded in its form).

When **lexeme** is present, it identifies the primary token: the English word that
directly translates the source lexeme. Words in **gloss** that do not appear in
**lexeme** are candidates for secondary classification.

## SBLGNT MORPH CODES

Format: POS-TENSE_VOICE_MOOD-CASE_NUMBER_GENDER

  Tense:  P=present  A=aorist  F=future  I=imperfect  X=perfect  Y=pluperfect
  Voice:  A=active  M=middle  P=passive
  Mood:   I=indicative  S=subjunctive  M=imperative  P=participle  N=infinitive  O=optative
  Case:   N=nominative  G=genitive  D=dative  A=accusative  V=vocative
  Number: S=singular  P=plural
  Gender: M=masculine  F=feminine  N=neuter
  POS prefix: V=verb  T=article  N=noun  A=adjective  D=adverb  P=pronoun
              C=conjunction  I=interjection  X=particle

Examples:
  V-PAI-3S  = verb, present active indicative, 3rd singular
  V-APP-NSM = verb, aorist passive participle, nominative singular masculine
  V-PAN     = verb, present active infinitive
  N-NSM     = noun, nominative singular masculine
  N-GPF     = noun, genitive plural feminine
  A-NSM     = adjective, nominative singular masculine
  A-GSN-C   = adjective, genitive singular neuter, comparative
  A-NSM-S   = adjective, nominative singular masculine, superlative
  T-NSM     = article, nominative singular masculine
  P-NSM     = pronoun, nominative singular masculine
  P-GSM     = pronoun, genitive singular masculine

## ARTICLES

Greek has a definite article (ὁ/ἡ/τό); English has both definite ("the") and indefinite
("a/an"). For every Greek article token (POS T-*), ask one question:

**Does this article have a specific English word as its direct correspondent?**

**YES → give it a primary 1:1 record for that word (see branch A below).**
**NO  → it is secondary to its head word. Never NEQ. Never omitted. Always secondary.**

**Critical prohibition: a Greek article NEVER corresponds to a preposition.**
English prepositions ("of", "to", "for", "with", "from", "in", "among", etc.) that
arise from a noun's or participle's case are secondary to that noun or participle —
not to the article. The article is either "the" (branch A) or secondary to its head
(branch B), regardless of what case it is in.

### Branch A — article has an English correspondent

- **→ "the":** 1:1 primary record for the article; noun/adjective/participle gets its
  own separate record.
  Example: ὁ λόγος → "the word":
    source=[ὁ], target=["the"] — primary 1:1
    source=[λόγος], target=["word"] — primary 1:1

- **→ possessive pronoun ("his", "her", "their", "its"):** 1:1 primary — but ONLY when
  no explicit Greek possessive pronoun (αὐτοῦ, αὐτῆς, αὐτῶν, μου, σου, ἡμῶν, etc.)
  is present. Greek uses the article with body parts and personal relationships where
  English supplies a possessive.
  Example (no explicit pronoun): τοὺς ὀφθαλμούς → "their eyes":
    source=[τούς],       target=["their"] — primary 1:1
    source=[ὀφθαλμούς], target=["eyes"]  — primary 1:1
  Counter-example (explicit αὐτῶν present): τοὺς ὀφθαλμοὺς αὐτῶν → "their eyes":
    source=[αὐτῶν],             target=["their"] — primary 1:1 (pronoun takes "their")
    source=[τούς, ὀφθαλμούς],  target=["eyes"]  — primary: "eyes"; secondary.source: [τούς]

- **→ "those" / "the one" (substantive participle):** When an article + participle
  forms a noun phrase and English renders it with "those who", "the one who", "whoever",
  etc., the article → "those"/"the one" (primary 1:1); the relative pronoun "who" is
  secondary to the participle. Any case-implied preposition ("to those who", "for those
  who") is secondary to the **participle**, not to the article.
  Example: τοῖς πιστεύουσιν → "to those who believe":
    source=[τοῖς],         target=["those"]          — primary 1:1
    source=[πιστεύουσιν],  target=["who", "believe"] — primary: "believe"; secondary: "who"
    "to" → secondary to πιστεύουσιν (dative case-implied)

### Branch B — article has no English correspondent → secondary to its head

Apply independently to each article in the verse. The head is always the word the
article grammatically modifies:

- **Articular noun, no English "the":** secondary to the noun.
  Example: τὴν χεῖρα → "hand":
    source=[τήν, χεῖρα], target=["hand"] — primary: "hand"; secondary.source: [τήν]

- **Attributive adjective:** secondary to the adjective (not the noun), applied to
  each article separately.
  Example: τὴν γῆν τὴν καλήν → "good soil":
    source=[τήν, γῆν],   target=["soil"] — primary: "soil"; secondary.source: [τήν]
    source=[τήν, καλήν], target=["good"] — primary: "good"; secondary.source: [τήν]

- **Articular infinitive:** secondary to the infinitive.
  Example: ἐν τῷ σπείρειν → "while sowing":
    source=[τῷ, σπείρειν], target=["sowing"] — primary: "sowing"; secondary.source: [τῷ]

### Anarthrous noun → English has "a/an"

No Greek article token exists; include the English "a/an" as a secondary target in
the noun's record.
  Example: ἄνθρωπος → "a man":
    source=[ἄνθρωπος], target=["a", "man"] — primary: "man"; secondary.target: ["a"]

## CONJUNCTIONS AND PARTICLES

When a conjunction or particle has a clear lexical correspondent in the translation,
align it. When the translation restructures and no correspondent exists, the conjunction
or particle → NEQ. When a translation word could plausibly align to either a
conjunction/particle or a content word, the content word has priority.

## IDIOMS

When a phrase-level correspondence has no token-level equivalent, use
meta.is_idiom: true. All tokens in the record are implicitly primary; meta.secondary
does not apply to idiom records.

**Idiom is a last resort.** Always prefer splitting a phrase into standard non-idiom
records before reaching for meta.is_idiom. If you can find a reasonable primary
correspondence for individual tokens — even if the match is loose — use standard records.
Use idiom only when no plausible token-level decomposition exists.

**Function-word records are never idioms.** When the source side of a record consists
entirely of conjunctions (POS C-*), particles (POS X-*), or prepositions — even when
aligning to multiple target tokens — do not mark it as an idiom. These function words
are semantically flexible and need not match their literal glosses to be primary
alignments. Instead, produce a standard record with the translation correspondent(s)
as primary tokens.

Example — καὶ ἐγένετο → "Now it came to pass":
  Wrong:  source=[καὶ, ἐγένετο], target=["Now","it","came","to","pass"], meta.is_idiom: true
  Better: source=[καὶ], target=["Now"] — primary 1:1 (καὶ here marks a narrative transition)
          source=[ἐγένετο], target=["it","came","to","pass"] — primary: "came"; secondary: "it", "to", "pass"

## VERBAL ASPECT

Greek aspect is encoded in the verb morphology, not as a separate token. When a
translator renders aspect explicitly — through an auxiliary or modal ("was doing",
"tried to", "began to") — both the aspect-expressing element and the main verb element
are primary to the single Greek verb. The Greek token carries the combined meaning;
the translation distributes it across words.

## ELLIPSIS OF εἶναι

When a copula ("is", "are", "was", "were") appears in the translation but no Greek
εἶναι token is present in the verse, the copula has no source correspondent → NEQ
target. This applies when the copula is genuinely supplied by the translator with no
implied Greek verb behind it. When εἶναι is present in the source, align normally.

## DISCOURSE RESTRUCTURING

Translations sometimes reorder clauses, shift boundaries, or change syntactic structure
relative to the Greek. Align tokens to their semantic correspondents regardless of word
order or clause position. Do not force artificial alignments to compensate for
restructuring — if no genuine correspondence exists, mark NEQ.\
"""

PASSIVE_BLOCK = """\
## PASSIVE VOICE

When a Greek passive verb is rendered with an auxiliary + past participle ("was sent",
"is written", "has been fulfilled"), the past participle is primary to the Greek verb.
The auxiliary ("was", "is", "has been") is secondary — it exists because Greek encodes
voice morphologically rather than through a separate word.

A subject pronoun supplied in the translation but absent from the Greek ("it was
written", "he was sent") is also secondary — the subject is implied by the verb's
person, number, and discourse context, not by a separate Greek token.

The "it" of a passive supplied subject is secondary, not NEQ — contrast the dummy "it"
of impersonal verbs (see IMPERSONAL VERBS below), which has no Greek correspondent at
all and is NEQ rather than secondary.

Example — γέγραπται → "it is written":
  source=[γέγραπται], target=["it", "is", "written"]
    primary: "written";  secondary: "is", "it"\
"""

IMPERSONAL_BLOCK = """\
## IMPERSONAL VERBS

Some Greek verbs are used impersonally — they have no real subject, only a grammatical
placeholder. Common examples: δεῖ ("it is necessary"), ἔξεστιν ("it is
lawful/permitted"), πρέπει ("it is fitting"), συμφέρει ("it is better/profitable"),
δοκεῖ ("it seems").

When these are rendered with a dummy subject "it" in the translation, that "it" has no
Greek correspondent — there is no implied subject behind it, only a grammatical
convention of English. The dummy "it" → NEQ target.

The complementary infinitive that typically follows (δεῖ + infinitive, "it is necessary
to do") is a separate alignment: the infinitive is primary to the Greek infinitive; "to"
is secondary. The impersonal verb itself aligns to its translation equivalent in the
normal way.

Contrast with the passive supplied subject (see PASSIVE VOICE above): a passive supplied
"it" is secondary because it represents the implied grammatical subject of the verb. An
impersonal "it" is NEQ because no subject — implied or otherwise — exists in the Greek.

Example — δεῖ → "it is necessary":
  source=[δεῖ], target=["is", "necessary"] — "is" and "necessary" both primary to δεῖ
  "it" → NEQ target (no Greek correspondent; no subject is implied)

  Or rendered compactly:
  source=[δεῖ], target=["must"] — primary\
"""

PARTICIPLE_BLOCK = """\
## PARTICIPIAL CONSTRUCTIONS

Greek participles are verbal adjectives. First identify the participle's syntactic role,
then apply the rule for that role.

**What syntactic function is the participle serving?**

### Adverbial (circumstantial)

The participle modifies the main verb, expressing time, cause, concession, or manner.
English renders it as a subordinate clause introduced by a conjunction or adverb
("when", "while", "after", "because", "although").

The introductory conjunction/adverb is **secondary** to the participle — it makes
explicit the logical relationship Greek encodes in the participle's aspect and context.
A supplied subject pronoun is secondary if implied by the participle's case agreement.

  source=[ἀκούσας], target=["when", "he", "heard"]
    primary: "heard";  secondary: "when", "he"

### Genitive absolute

The participle and its genitive nominal element together express a circumstantial idea
external to the main clause. Align each element to its translation correspondent.
Supplied conjunctions or adverbs introducing the rendered clause are secondary to the
participle.

  source=[αὐτοῦ],     target=["he"]                      — primary (explicit subject)
  source=[λαλοῦντος], target=["while", "was", "speaking"]
    primary: "speaking";  secondary: "while", "was"

### Substantive

The participle functions as a noun phrase. Apply ARTICLES rules to the article if
present (→ "the"/"those" if English has it; secondary to participle otherwise).
Relative pronouns or connectors ("who", "that", "which") introduced in English are
secondary to the participle.

  source=[πιστεύων], target=["whoever", "believes"]
    primary: "believes";  secondary: "whoever"

### Discourse particle adjacent to a participle

When δέ, καί, οὖν or similar appears near a participle but has no correspondent in the
participle's rendering, consider NEQ — only when confident the particle has no
translation equivalent anywhere in the surrounding clause.\
"""

INFINITIVE_BLOCK = """\
## INFINITIVAL CONSTRUCTIONS

The Greek infinitive is a verbal noun. Translations typically render it with "to" +
verb, but the infinitive's function in the clause shapes how surrounding words align.

### Complementary infinitive

After verbs of ability, necessity, or desire (δύναμαι, θέλω, and similar), the
infinitive completes the main verb's meaning. The infinitive is primary to the Greek
infinitive; "to" is secondary — it is an English grammatical marker with no separate
Greek correspondent.

Example — θέλω ἐλθεῖν → "I want to come":
  source=[ἐλθεῖν], target=["to", "come"]
    primary: "come";  secondary: "to"

### Articular infinitive

When an infinitive is preceded by an article (τό, τῷ, τοῦ), the article marks the
infinitive's case function in the clause. English has no separate word for this article,
so the article is **secondary to the infinitive** — it does not get its own record and
is never NEQ.

Prepositions governing the articular infinitive (εἰς τό, ἐν τῷ, πρὸς τό) align to
their English correspondents as primary records. Auxiliaries rendered from the
infinitive's aspect ("was sowing", "to be doing") are secondary to the infinitive.

Example — ἐν τῷ σπείρειν αὐτόν → "as he was sowing":
  source=[ἐν],            target=["as"]              — primary (preposition)
  source=[αὐτόν],         target=["he"]              — primary (accusative subject)
  source=[τῷ, σπείρειν],  target=["was", "sowing"]
    primary: "sowing";  secondary.source: [τῷ];  secondary.target: ["was"]

### Purpose and result infinitive (without ἵνα)

When an infinitive expresses purpose or result directly ("he came to save", "so as to
fulfill"), the "to" is secondary to the infinitive. If an English conjunction introduces
the infinitive phrase ("in order to", "so as to"), that conjunction is secondary to the
infinitive — it makes explicit a relationship the Greek encodes through the infinitive's
function alone.

Example — ἦλθεν σῴζειν → "he came to save":
  source=[σῴζειν], target=["to", "save"]
    primary: "save";  secondary: "to"

### Indirect discourse

An infinitive in indirect discourse (after verbs of saying, believing, knowing) aligns
to its translation correspondent. Supplied conjunctions ("that") introducing the
indirect statement are secondary to the governing verb, not the infinitive.

Example — λέγει αὐτὸν εἶναι → "says that he is":
  source=[λέγει],  target=["says"]        — primary
  source=[αὐτόν], target=["he"]           — primary
  source=[εἶναι], target=["that", "is"]
    primary: "is" (infinitive → finite verb in English);  secondary: "that"\
"""

HINA_BLOCK = """\
## ἵνα CLAUSES

ἵνα introduces purpose or result clauses and is one of the more translation-variable
Greek particles. How it is rendered determines how it aligns.

### ἵνα rendered as a purpose clause

When ἵνα is rendered as a purpose or result conjunction ("in order that", "so that",
"that"), the conjunction is primary to ἵνα — it exists because of ἵνα's purpose force.
The verbs and other content words in the clause align to their Greek correspondents
normally.

### ἵνα rendered as a bare "to" used with an infinitive

When ἵνα is rendered as a bare "to" used with an infinitive, that "to" is primary to
ἵνα — not secondary to the infinitive. The practical test: this "to" exists because of
ἵνα's purpose force, not merely as an English infinitive marker. The infinitive itself
aligns to the Greek verb in the ἵνα clause normally.

### ἵνα with no explicit translation correspondent

When a translator absorbs ἵνα's force into the surrounding structure without a distinct
conjunction or infinitive marker, ἵνα → NEQ source. Apply this only when you are
confident no translation element corresponds to ἵνα's purpose or result force.

Example — ἵνα σωθῇ → "that he might be saved":
  source=[ἵνα], target=["that"] — primary 1:1

Example — ἵνα σῴζῃ → "to save" (bare infinitive rendering):
  source=[ἵνα],   target=["to"]   — primary (purpose marker, not an infinitive marker)
  source=[σῴζῃ], target=["save"] — primary\
"""

COMPARATIVE_BLOCK = """\
## COMPARATIVES AND SUPERLATIVES

Greek comparatives and superlatives are encoded morphologically in the adjective or
adverb (degree marker `-C` for comparative, `-S` for superlative). Translations
typically render them with degree words ("more", "most", "less", "least", "better",
"best", "greater", "greatest") or suffixes ("-er", "-est").

### Comparative

When a Greek comparative adjective or adverb is rendered with a degree word + base form
("more clearly", "greater than"), both the degree word ("more", "greater") and the base
form ("clearly") are primary to the single Greek comparative token — the Greek encodes
degree morphologically; English distributes it across words.

The standard of comparison ("than X") aligns to the Greek construction expressing it
(ἤ + noun, or genitive of comparison). "Than" is secondary to the noun or adjective
it governs in the comparison.

### Superlative

The same principle applies: degree word + base form ("most clearly", "greatest") are
both primary to the single Greek superlative token. An elative superlative (very +
adjective, "very great") follows the same pattern.

Example — μείζων → "greater":
  source=[μείζων], target=["greater"] — primary 1:1

Example — ἁγιώτατος → "most holy":
  source=[ἁγιώτατος], target=["most", "holy"] — both primary\
"""

AUTOS_BLOCK = """\
## αὐτός

First identify the grammatical function αὐτός is serving in the clause:

**What is αὐτός doing here?**

### Intensive (attributive position — adds emphasis to a noun or pronoun)

αὐτός stands beside a noun/pronoun to emphasize it ("the man himself", "Jesus
himself"). Align to the intensive pronoun; the noun it modifies gets its own record.

  source=[αὐτός],   target=["himself"] — primary 1:1
  source=[Ἰησοῦς], target=["Jesus"]   — primary 1:1 (separate record)

### Reflexive (object refers back to the subject)

αὐτός functions as a reflexive pronoun. Align to the reflexive in translation
("himself", "herself", "themselves").

  source=[αὐτόν], target=["himself"] — primary 1:1

### Third-person pronoun (most common use)

αὐτός serves as a simple third-person pronoun. Align to the corresponding pronoun
("him", "her", "it", "them", "his", "her", "their"). When the translation substitutes
a proper name for clarity, the name is primary to αὐτός; any additionally supplied
subject pronoun is secondary.

  source=[αὐτόν], target=["him"]   — primary 1:1
  source=[αὐτοῦ], target=["Jesus"] — primary (name substituted for pronoun)

### No translation correspondent

αὐτός is absorbed into surrounding structure or stylistically omitted → NEQ source,
but only when confident no translation element corresponds to it.\
"""

HOTI_BLOCK = """\
## ὅτι

ὅτι serves two distinct functions in Greek, and its alignment depends on which it
is performing.

### ὅτι as conjunction ("that", "because", "for")

When ὅτι introduces indirect discourse or a causal clause, its translation
correspondent ("that", "because", "for") is primary to ὅτι. When the translation
omits the conjunction and moves directly into the indirect statement or clause,
ὅτι → NEQ source.

### ὅτι as quotation marker (recitativum)

When ὅτι introduces direct speech (ὅτι recitativum), it functions as a quotation
marker with no translation equivalent — English uses punctuation (a colon or
quotation marks) rather than a word. In this use ὅτι → NEQ source.

### Distinguishing the two

The function is usually clear from context: ὅτι recitativum follows a verb of saying
or asking and introduces direct speech; ὅτι as conjunction introduces an indirect
statement or a causal clause. When the distinction is genuinely ambiguous, prefer the
conjunction reading and align if a correspondent exists.

Example — ὅτι (conjunction) → "that":
  source=[ὅτι], target=["that"] — primary 1:1

Example — ὅτι (recitativum) → quotation marks only:
  source=[ὅτι] → NEQ source\
"""

CONDITIONAL_BLOCK = """\
## CONDITIONAL CONSTRUCTIONS

Greek conditionals are introduced by εἰ (simple or contrary-to-fact) or ἐάν (with
subjunctive, more probable). The alignment of the conditional elements follows the
general conjunction guidelines, but several features of conditional sentences are
worth noting.

### The conditional particle

εἰ and ἐάν typically correspond to "if" in translation, but translators render
conditional force in many ways — "unless", "even if", "whether", "when", and others.
Look for the translation element that carries the conditional or hypothetical force
of the clause and align to that. When you are confident no translation element
corresponds to the conditional particle's force, NEQ source is appropriate.

### Apodosis markers

Greek sometimes introduces the apodosis (the "then" clause) with a particle (τότε,
ἄρα, οὖν) or leaves it unmarked. When the translation supplies "then" with no Greek
correspondent, "then" → NEQ target. When a Greek apodosis particle is present, align
it to its translation correspondent if one exists.

### Contrary-to-fact conditions

εἰ with indicative in past tense (contrary-to-fact) may be rendered with modal
auxiliaries ("would have", "could have") in the apodosis. Those auxiliaries are
primary to the Greek verb they render — Greek encodes counterfactuality through mood
and tense rather than separate modal words; English distributes that meaning across
words.

### Everything else

Supplied pronouns, helping verbs, conjunctions, and particles within the protasis and
apodosis follow the general guidelines for those constructions.

Example — εἰ → "if":
  source=[εἰ], target=["if"] — primary 1:1

Example — contrary-to-fact apodosis verb → "would have known":
  source=[verb], target=["would", "have", "known"]
    all three primary — Greek encodes counterfactuality through mood and tense;
    English distributes that meaning across modal + auxiliary + main verb\
"""


NEGATION_BLOCK = """\
## NEGATION

### Simple negation

Greek negation particles (οὐ, οὐκ, οὐχ, μή and related forms) are discrete tokens
separate from the verb they negate. Look for the English "not" or "no" and align it
directly to the negation particle as a **primary** 1:1 record.

The negated verb aligns to its English correspondent — main verb and auxiliaries —
**without** including "not." Because English places "not" between the auxiliary and
the main verb, the verb record is typically **discontiguous**: the auxiliary and main
verb are non-adjacent target tokens (listed in document order), with "not" belonging
to its own record between them. This discontiguous verb record is expected and correct.

Do not include "not" as a secondary token within the verb record. It has its own
source token and belongs in its own record.

Example — οὐκ ἔρχεται → "is not coming":
  source=[οὐκ],       target=["not"]              — primary 1:1
  source=[ἔρχεται],  target=["is", "coming"]
    primary: "coming";  secondary: "is"
    (discontiguous — "not" intervenes in English but belongs to the negation record)

### Emphatic negation

οὐ μή + subjunctive expresses strong emphatic negation. Translations render it as
"will never," "certainly not," "by no means," or similar. Both particles are
**primary** in a single record against the emphatic English expression. "will" belongs
in the verb record as secondary, not in the negation record.

Example — οὐ μή + subjunctive verb → "will never [verb]":
  source=[οὐ, μή],  target=["never"]            — both particles primary
  source=[verb],    target=["will", "[verb]"]
    primary: main verb;  secondary: "will"

### Compound negation tokens

Some Greek forms are single tokens encoding negation together with another element.
All English words in the rendered phrase are **primary** to the single Greek token:

- οὐδέ / μηδέ ("and not," "neither," "nor") — single token aligns to full phrase
- οὐκέτι / μηκέτι ("no longer," "no more") — single token aligns to full phrase
- οὔπω / μήπω ("not yet") — single token aligns to full phrase
- οὔτε ("neither … nor," correlative) — aligns to whichever element corresponds

Example — οὐκέτι → "no longer":
  source=[οὐκέτι], target=["no", "longer"] — both primary

### Negation with negative pronouns

When a clause contains both a negative pronoun (οὐδείς → "nobody," "no one";
μηδείς → "no one," "nothing") and emphatic negation (οὐ μή), Greek double negation
is emphatic, not canceling. English absorbs the extra negation into "ever" or "at all."

- οὐδείς / μηδείς → "nobody" / "no one" / "nothing" — primary
- οὐ + μή → "ever" / "at all" — both primary in a single record
- Main verb auxiliaries → secondary to the verb, not part of the negation record\
"""


# ---------------------------------------------------------------------------
# Block registry
# ---------------------------------------------------------------------------

# Canonical inclusion order for conditional blocks
BLOCK_ORDER = [
    "PASSIVE",
    "IMPERSONAL",
    "PARTICIPLE",
    "INFINITIVE",
    "HINA",
    "COMPARATIVE",
    "AUTOS",
    "HOTI",
    "CONDITIONAL",
    "NEGATION",
]

CONDITIONAL_BLOCKS: dict[str, str] = {
    "PASSIVE":     PASSIVE_BLOCK,
    "IMPERSONAL":  IMPERSONAL_BLOCK,
    "PARTICIPLE":  PARTICIPLE_BLOCK,
    "INFINITIVE":  INFINITIVE_BLOCK,
    "HINA":        HINA_BLOCK,
    "COMPARATIVE": COMPARATIVE_BLOCK,
    "AUTOS":       AUTOS_BLOCK,
    "HOTI":        HOTI_BLOCK,
    "CONDITIONAL": CONDITIONAL_BLOCK,
    "NEGATION":    NEGATION_BLOCK,
}

# When a tag is detected, these additional tags are always included
FORCED_INCLUSIONS: dict[str, set[str]] = {
    "PASSIVE": {"IMPERSONAL"},
    "HINA":    {"INFINITIVE"},
}

# Text forms that identify impersonal verb uses (expand as needed)
_IMPERSONAL_FORMS: frozenset[str] = frozenset({
    "δεῖ", "ἔξεστιν", "ἔξεστι", "πρέπει", "συμφέρει", "δοκεῖ",
})

# Negation particle text forms (simple, emphatic, and compound single-token)
_NEGATION_FORMS: frozenset[str] = frozenset({
    "οὐ", "οὐκ", "οὐχ", "οὐχί",
    "μή", "μήτε",
    "οὐδέ", "μηδέ",
    "οὐκέτι", "μηκέτι",
    "οὔπω", "μήπω",
    "οὔτε",
})


# ---------------------------------------------------------------------------
# Phenomenon detection
# ---------------------------------------------------------------------------

def detect_phenomena(source_tokens: list[Source]) -> set[str]:
    """Scan source token POS/morph fields and return a set of phenomenon tags.

    Tags correspond to keys in CONDITIONAL_BLOCKS.  Forced co-inclusions are
    NOT applied here — that happens in build_system_prompt().
    """
    tags: set[str] = set()

    for t in source_tokens:
        morph = t.morph or ""
        text = t.text or ""
        lemma = t.lemma or ""

        if t.pos == "verb" and "-" in morph:
            # Split on "-": parts[1] is the TAM block (e.g. "PAI", "APP", "2AAN")
            # Reading voice and mood from the END of the TAM block handles both
            # standard 3-char codes and extended 4-char 2nd-aorist codes (e.g. "2APP").
            tam = morph.split("-")[1]
            if len(tam) >= 3:
                voice = tam[-2]
                mood  = tam[-1]
                if voice == "P":
                    tags.add("PASSIVE")
                if mood == "P":
                    tags.add("PARTICIPLE")
                if mood == "N":
                    tags.add("INFINITIVE")

        if t.pos in ("adj", "adv"):
            if morph.endswith("-C") or morph.endswith("-S"):
                tags.add("COMPARATIVE")

        if text == "ἵνα":
            tags.add("HINA")
        if text in ("εἰ", "ἐάν"):
            tags.add("CONDITIONAL")
        if text == "ὅτι":
            tags.add("HOTI")
        if lemma == "αὐτός" or text in ("αὐτός", "αὐτοῦ", "αὐτῷ", "αὐτόν",
                                         "αὐτή", "αὐτῆς", "αὐτῇ", "αὐτήν",
                                         "αὐτό", "αὐτοί", "αὐτῶν", "αὐτοῖς",
                                         "αὐτούς", "αὐταί", "αὐτάς"):
            tags.add("AUTOS")
        if text in _IMPERSONAL_FORMS:
            tags.add("IMPERSONAL")
        if text in _NEGATION_FORMS:
            tags.add("NEGATION")

    return tags


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------

def build_system_prompt(phenomena: set[str], target_language: str) -> str:
    """Assemble the system prompt from the base block plus relevant conditional blocks.

    Applies forced co-inclusions before selecting blocks.  Blocks are included
    in BLOCK_ORDER regardless of detection order.

    Args:
        phenomena: Tags returned by detect_phenomena().
        target_language: ISO 639-3 code (e.g. "eng").  Currently unused but
            reserved for future gloss-visibility logic at the system-prompt level.
    """
    # Apply forced co-inclusions
    expanded: set[str] = set(phenomena)
    for tag, forced in FORCED_INCLUSIONS.items():
        if tag in expanded:
            expanded |= forced

    blocks = [BASE_BLOCK]

    if expanded:
        active = [t for t in BLOCK_ORDER if t in expanded]
        notice = (
            "The following constructions were identified in this verse batch. "
            "Specific guidelines for each are included below: "
            + ", ".join(active) + "."
        )
        blocks.append(notice)

    for tag in BLOCK_ORDER:
        if tag in expanded:
            blocks.append(CONDITIONAL_BLOCKS[tag])

    return "\n\n---\n\n".join(blocks)


# ---------------------------------------------------------------------------
# Per-verse message formatting
# ---------------------------------------------------------------------------

def _format_source_token(token: Source, show_gloss: bool) -> str:
    """Return a single formatted source token line."""
    parts = [f"  {token.id:<16}", token.text, f"  {token.pos:<6}", token.morph or ""]
    if show_gloss and token.gloss:
        parts.append(f'  gloss:"{token.gloss}"')
        if token.gloss2 and token.gloss2 != token.gloss:
            # WLCM uses '.' as word delimiter in gloss2; normalize to space
            lexeme = token.gloss2.replace(".", " ")
            parts.append(f'  lexeme:"{lexeme}"')
    return "".join(parts)


def format_verse_block(
    verse_id: str,
    source_tokens: list[Source],
    target_tokens: list[MigrateTarget],
    candidates: dict[str, list[dict]],
    target_language: str,
) -> str:
    """Return the formatted text block for one verse within a batch message.

    Args:
        verse_id: 8-digit BCV string, e.g. "41004003".
        source_tokens: Ordered source tokens for this verse.
        target_tokens: Ordered target tokens for this verse.
        candidates: Mapping of source-type label → list of record dicts, each
            with ``"source": [str, ...]`` and ``"target": [str, ...]``.
        target_language: ISO 639-3 code; glosses are shown only for "eng".
    """
    show_gloss = target_language == "eng"
    lines: list[str] = []

    lines.append(f"--- VERSE {verse_id} ---")
    lines.append("")

    lines.append("SOURCE TOKENS (SBLGNT):")
    for t in source_tokens:
        lines.append(_format_source_token(t, show_gloss))
    lines.append("")

    lines.append("TARGET TOKENS:")
    for t in target_tokens:
        lines.append(f"  {t.id:<16}  {t.text!r}")
    lines.append("")

    lines.append("ALIGNMENT CANDIDATES:")
    if not candidates:
        lines.append("  (none)")
    else:
        for source_type, records in candidates.items():
            lines.append(f"\n[{source_type}]")
            for rec in records:
                src_ids = " ".join(rec.get("source", []))
                tgt_ids = " ".join(rec.get("target", []))
                lines.append(f"  source: [{src_ids}]  target: [{tgt_ids}]")

    return "\n".join(lines)


def build_batch_message(
    verse_batch: list[tuple[str, list[Source], list[MigrateTarget], dict[str, list[dict]]]],
    target_language: str,
) -> str:
    """Concatenate verse blocks for a batch into a single user message.

    Args:
        verse_batch: List of (verse_id, source_tokens, target_tokens, candidates)
            tuples, one per verse.
        target_language: ISO 639-3 code passed through to format_verse_block().
    """
    return "\n\n".join(
        format_verse_block(vid, src, tgt, cands, target_language)
        for vid, src, tgt, cands in verse_batch
    )
