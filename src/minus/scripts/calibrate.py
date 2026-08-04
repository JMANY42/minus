"""
calibrate_thresholds.py

Purpose has changed from the old free-sentence version: dedupe/conflict in
memory_store.py is now exact-match on (attribute, value), so there's no
dup_threshold/conflict_threshold left to tune for that.

The only place cosine similarity still matters is search_facts() --
retrieving facts relevant to a new conversational message. This script
calibrates a RELEVANCE THRESHOLD for that: the minimum similarity score at
which a retrieved fact is worth injecting into context, vs. noise that
happens to rank in the top-k but isn't actually relevant.

It tests query -> raw_text pairs using the new canonical "attribute: value"
format (the same templated string add_fact() generates and embeds), grouped
into:

  1. direct_match   -- a natural question that's clearly asking about this
                        exact attribute. Should score HIGH.
  2. related_topic   -- a query that's topically nearby but asking about a
                        DIFFERENT attribute (e.g. "what editor do you use"
                        vs a "diet" fact). Should score LOW-MEDIUM -- these
                        are the pairs most likely to cause false positives
                        if your relevance threshold is set too low.
  3. unrelated       -- a query with no real connection to the fact. Should
                        score LOWEST, confirming there's a real gap to work
                        with at all.

Run it, look at the mean/stdev per group, and set a relevance_threshold
(suggested at the end) for filtering search_facts() results in your agent
loop, e.g.:

    facts = store.search_facts(user_message, top_k=10)
    facts = [f for f in facts if f.similarity >= relevance_threshold]

Add your own (query, raw_text) pairs matching your actual attributes/values
for the most accurate calibration -- the ones below are just a starting point.
"""

import statistics
import struct

from minus.memory.facts.embeddings import SentenceTransformerEmbedder
from minus.memory.facts.models import default_raw_text

_embedder = SentenceTransformerEmbedder()
embed = _embedder.embed


def cosine_similarity_from_embeddings(a: bytes, b: bytes) -> float:
    """Both a and b are the raw bytes produced by embed(). Unpack and compute
    cosine similarity directly in Python (no DB needed for calibration)."""
    n = len(a) // 4
    va = struct.unpack(f"{n}f", a)
    vb = struct.unpack(f"{n}f", b)
    dot = sum(x * y for x, y in zip(va, vb))
    norm_a = sum(x * x for x in va) ** 0.5
    norm_b = sum(x * x for x in vb) ** 0.5
    return dot / (norm_a * norm_b)


# ---- Example (query, attribute, value) triples. Replace/add ones that match
# YOUR actual attributes and how a user would naturally ask about them. ----

direct_match = [
    ("what timezone are you in?", "timezone", "PST"),
    ("what editor do you use?", "preferred_editor", "VS Code"),
    ("what's your job?", "job_title", "Software Engineer"),
    ("are you allergic to anything?", "allergy", "peanuts"),
    ("what city do you live in?", "home_city", "Austin"),
    ("what language do you code in?", "preferred_language", "Python"),
    ("do you have any kids?", "num_children", "2"),
    ("what car do you drive?", "car", "Tesla"),
    ("what's your diet like?", "diet", "vegetarian"),
    ("when do you wake up?", "wake_time", "6am"),
    ("what's the name of your startup?", "company_name", "Nova"),
    ("how do you like your coffee?", "coffee_order", "black, no sugar"),
    ("what's your preferred way to communicate?", "communication_preference", "email"),
    ("what operating system do you use?", "os", "macOS"),
    ("what's your favorite hobby?", "hobby", "climbing"),
]

related_topic = [
    ("what timezone are you in?", "home_city", "Austin"),
    ("what editor do you use?", "preferred_language", "Python"),
    ("what's your job?", "company_name", "Nova"),
    ("are you allergic to anything?", "diet", "vegetarian"),
    ("what city do you live in?", "timezone", "PST"),
    ("what language do you code in?", "preferred_editor", "VS Code"),
    ("do you have any kids?", "wake_time", "6am"),
    ("what car do you drive?", "os", "macOS"),
    ("what's your diet like?", "allergy", "peanuts"),
    ("when do you wake up?", "num_children", "2"),
    ("what's the name of your startup?", "job_title", "Software Engineer"),
    ("how do you like your coffee?", "communication_preference", "email"),
    ("what's your preferred way to communicate?", "coffee_order", "black, no sugar"),
    ("what operating system do you use?", "preferred_editor", "VS Code"),
    ("what's your favorite hobby?", "car", "Tesla"),
]

unrelated = [
    ("what timezone are you in?", "hobby", "climbing"),
    ("what editor do you use?", "car", "Tesla"),
    ("what's your job?", "coffee_order", "black, no sugar"),
    ("are you allergic to anything?", "os", "macOS"),
    ("what city do you live in?", "hobby", "climbing"),
    ("what language do you code in?", "diet", "vegetarian"),
    ("do you have any kids?", "company_name", "Nova"),
    ("what car do you drive?", "allergy", "peanuts"),
    ("what's your diet like?", "wake_time", "6am"),
    ("when do you wake up?", "car", "Tesla"),
    ("what's the name of your startup?", "coffee_order", "black, no sugar"),
    ("how do you like your coffee?", "num_children", "2"),
    ("what's your preferred way to communicate?", "os", "macOS"),
    ("what operating system do you use?", "hobby", "climbing"),
    ("what's your favorite hobby?", "timezone", "PST"),
]


def score_group(triples, label):
    print(f"\n--- {label} ---")
    scores = []
    for query, attribute, value in triples:
        raw_text = default_raw_text(attribute, value)
        sim = cosine_similarity_from_embeddings(embed(query), embed(raw_text))
        scores.append(sim)
        print(f"  {sim:.3f}  |  \"{query}\"  vs  \"{raw_text}\"")
    if len(scores) >= 2:
        mean = statistics.mean(scores)
        stdev = statistics.stdev(scores)
        print(f"  n={len(scores)}  mean={mean:.3f}  stdev={stdev:.3f}  range=[{min(scores):.3f}, {max(scores):.3f}]")
    return scores


def run_calibration() -> None:
    """Print similarity distributions and a suggested relevance threshold."""
    direct_scores = score_group(direct_match, "Direct match (query clearly asks about this attribute)")
    related_scores = score_group(related_topic, "Related topic, different attribute (highest false-positive risk)")
    unrelated_scores = score_group(unrelated, "Unrelated")

    print("\n--- Summary statistics ---")
    for label, scores in [("Direct match", direct_scores),
                           ("Related, different attribute", related_scores),
                           ("Unrelated", unrelated_scores)]:
        if len(scores) >= 2:
            mean = statistics.mean(scores)
            stdev = statistics.stdev(scores)
            print(f"  {label:30s} mean={mean:.3f}  stdev={stdev:.3f}  "
                  f"(1 stdev band: [{mean - stdev:.3f}, {mean + stdev:.3f}])")

    print("\n--- Suggested relevance_threshold ---")
    if direct_scores and related_scores:
        d_mean, d_std = statistics.mean(direct_scores), statistics.stdev(direct_scores)
        r_mean, r_std = statistics.mean(related_scores), statistics.stdev(related_scores)
        suggested = (d_mean - d_std + r_mean + r_std) / 2
        print(f"relevance_threshold ~= {suggested:.3f}")
        print(f"   (direct-match mean-1std = {d_mean - d_std:.3f}, related-topic mean+1std = {r_mean + r_std:.3f})")
        if d_mean - d_std <= r_mean + r_std:
            print("   WARNING: direct-match and related-topic distributions overlap within 1 stdev.")
            print("   That means search_facts() will sometimes surface a fact for the WRONG attribute")
            print("   just because it's topically nearby (e.g. a 'diet' fact showing up for an")
            print("   'allergy' question). Consider: (a) adding more query variety per attribute to")
            print("   check if this holds up, (b) also returning `attribute` alongside search results")
            print("   so your agent can sanity-check the match, or (c) accepting some false positives")
            print("   since showing an extra, slightly-off fact is usually cheaper than missing one.")

    print("\nNote: unlike the old dedupe thresholds, getting this exactly right matters less --")
    print("worst case here is injecting one irrelevant fact into context, not silently losing data.")
    print("A reasonable default if you don't want to calibrate further: keep relevance_threshold low")
    print("and rely on top_k to bound how much gets injected, rather than a strict cutoff.")
