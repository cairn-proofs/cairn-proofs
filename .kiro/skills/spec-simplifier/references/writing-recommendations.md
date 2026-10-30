# Writing Recommendations

Apply these rules when simplifying a spec. Order of sections below:

1. Formatting best practices
2. Basics (spelling, grammar)
3. Structure and form
4. Be concise (8 strategies)
5. Paragraph as the unit of composition
6. Active voice
7. Simple, specific words
8. Remove indirect language
9. Remove vague language
10. Remove weasel words
11. Positive and inclusive language
12. Remove adverbs and adjectives

---

## 1. Formatting best practices

1. **Acronyms** — Spell out on first use, e.g., Customer Service Associate (CSA); use CSA after. Assume readers are unfamiliar with abbreviations. Limit acronym count — too many cause confusion.
2. **Bullets and numbered lists** — Prefer numbered lists. Use bullets only where a numbered list could mislead. Use lists only for lists of items. Order by importance, most important first. List items should be no longer than a sentence or two, maximum. If multiple items in a list are more than one sentence long, you should probably use sub-headings instead.
3. **Headings and subheadings** — Use them to structure the narrative and guide the reader. Headings are sentence cased.
4. **Length** — As short as possible and as long as necessary. Run-on sentences and paragraphs are not acceptable.
5. **Title** — Include the document name and the date of the review or draft.
6. **White space** — Add white space between paragraphs; keep headings with their paragraphs.
7. **Line length** - Let lines wrap naturally. Don't add hard line-breaks unless required by the format you're targeting.
8. **Links** - HREFs and anchor links make it easier for readers to navigate references. Use github-slugger to compute links and make sure they're not concealed by other formatting.

## 2. Basics

Get spelling and grammar right. Errors distract the reader from your ideas and undermine your credibility.

## 3. Structure and form

Every document must state a clear purpose and fulfill it. Nothing more, nothing less. State the objective in the first paragraph, end with a recommendation, and identify next steps. If you have appendices, make them as well-written as the rest of the document. 

## 4. Be concise

Be a ruthless editor. For every paragraph, sentence, and clause, ask "why is this here?" and "how does it clarify the requirements?" If it doesn't, delete it.

### 4.1 Redundant pairs

Examples: full and complete, each and every, hopes and dreams, whole entire, first and foremost, true and accurate, always and forever.

When the first word in a pair has roughly the same meaning as the second, choose one.

- Before: "For each and every book you purchase, you will receive a free bookmark."
- After: "For each book you purchase, you will receive a free bookmark."

### 4.2 Unnecessary qualifiers

Examples: actually, really, basically, probably, very, definitely, somewhat, kind of, extremely, practically.

Deleting unnecessary qualifiers removes a word or two per sentence. Small tweaks add up.

- Before: "Because a great many of the words in this sentence are basically unnecessary, it would really be a very good idea to edit somewhat for conciseness."
- After: "Because many of the words in this sentence are unnecessary, it would be a good idea to edit for conciseness."

### 4.3 Prepositional phrases

Modifying phrases beginning with in, for, at, on, through, over. They can make a sentence clunky. Circle the prepositions and cut phrases that don't lose meaning.

- Before: "She caught the bus on time."
- After: "She caught the bus."

### 4.4 Unnecessary modifiers

Sometimes a word's meaning implies its modifier, making the modifier redundant. "Imply" already means to state indirectly, so "indirectly" is redundant.

- Before: "Do not try to anticipate in advance the way customers will respond."
- After: "Do not try to anticipate the way customers will respond."

### 4.5 Replace a phrase with a word

Many phrases reduce to a single word and often detract from meaning.

- "The reason for", "due to the fact that", "in light of the fact that", "given the fact that", "considering the fact that" → because, since, so, or why.
- "In the event that", "under circumstances in which" → if.
- "It is necessary that", "cannot be avoided" → must or should.
- "For the purpose of [-ing verb]" → "to [infinitive verb]".

### 4.6 Positives over negatives

Examples to watch: not, never, without. Negative form needs an extra word and makes readers work harder.

- Before: "If you do not have more than two years of experience, you can not apply for this role."
- After: "If you have more than two years of experience, you can apply for this role."

### 4.7 Repetition

We clarify our thinking as we write, which can make sentences repeat earlier thoughts. Reread and cut sentences or phrases that say the same thing.

- Before: "Our current staffing problems have been exacerbated by a combination of low salaries and the rising cost of housing. Because it has become more expensive for employees to rent or buy homes in our target cities, we are not able to attract employees with our current salaries, which leads to staffing difficulties."
- After: "Our current staffing problems have been exacerbated by a combination of low salaries and the rising cost of housing. Because it has become more expensive for employees to rent or buy homes in our target cities."

### 4.8 Just do it

Don't announce what you're about to do — do it. Introductory phrases like "we are going to define…" or "I want to point out that…" are unnecessary.

- Before: "I want to point out that there are two limitations to this plan."
- After: "There are two limitations to this plan."

## 5. Make the paragraph the unit of composition

Every paragraph should be indivisible and have a single clear topic. Start with a topic sentence, follow with sentences that explain or build on it, and close with a sentence that emphasizes the topic or its consequences. Run-on or divisible paragraphs must be decomposed. 

## 6. Use the active voice

In the active voice the subject acts on the object ("Steve chopped wood"). In the passive voice the subject is acted upon ("The wood was chopped by Steve"), and the object is optional ("The wood was chopped"). Passive is useful when the actor is unknown, but active voice gives more clarity. Use active more, passive less.

- Preferred: "We found page load time impacted conversion…"
- Less preferred: "We found that conversion has been impacted by page load time…"
- Preferred: "We will reduce latency by…"
- Less preferred: "A program of improvements will be implemented to reduce latency."

## 7. Use simple words and be specific

Generalizations produce weak statements. Specific statements in simple words are clear and powerful.

- Prefer "We found page load time impacted conversion…" to "It has become apparent that conversion has been impacted by page load time…".
- Prefer "We will reduce latency by…" to "A program of improvements will be implemented to reduce latency."

## 8. Remove indirect language

Indirect language tries to sound formal, polite, or soft: believe, in general, planning on, should, would like to. Remove it. Direct writing is more concise and earns reader trust.

- Do: "This is the correct solution."
- Don't: "I believe this is the correct solution."

## 9. Remove vague language

Vague language speaks about something without saying what it is: a number of, complex, generally, often, probably, should. Replace with data or remove it.

- Do: "This happens 75% of the time…"
- Don't: "This happens often…"
- Do: "We are off course…"
- Don't: "We are a bit off course…"

## 10. Remove weasel words

Weasel words turn out empty on analysis, especially those about timing and quantity: should, might, could, often, generally, usually, probably, significant, better, worse, soon, some, most, fewer, faster, slower, higher, lower, many, few, more. Replace with data or a statement of intent.

- Prefer "This will affect 23% of sellers" to "A significant proportion of sellers may be affected."
- Prefer "We will increase operating margin by 260bps" to "Implementing […] should result in a higher operating margin."

> Spec caution: when these words are RFC 2119 normative keywords (MUST, SHALL, SHOULD, MAY), they are load-bearing requirement levels — keep them. Apply this rule only to vague prose hedging.

## 11. Use positive and inclusive language

State what you want to happen. Make the desired state, action, and outcome simple to understand.

- Instead of: "We can't continue to show the wrong thing to customers and make these kinds of mistakes. If we don't fix this, we'll miss our OP2 goal."
- Use: "If we fix these problems, we'll show correct information to customers and achieve our OP2 goal."
- Instead of: "This is not a good use of resources." Use: "This is a poor use of resources."
- Instead of: "The data does not conclusively show…" Use: "The data is inconclusive."

Why positive over negative:

1. Conciseness — positive often uses fewer words.
2. Audience buy-in — negative language can read as defensive; positive language builds trust.
3. Multiple negatives confuse — "That's not to say the design is not without merit" has three negatives. Positive language avoids this and speeds comprehension.

Use the negative for denial or antithesis, never for evasion. Antithesis gives a powerful structure: "We are customer obsessed, not customer driven."

## 12. Remove adverbs and adjectives

Adjectives modify nouns ("the **short** document"). Adverbs modify verbs, adjectives, or other adverbs ("the document **quickly** provides"). In business writing, prefer data and actuals over modifiers.

- Instead of: "Sales grew quickly over the last quarter."
- Use: "Sales grew 450bps from $XX to $YY in Q4."

Adverbs and adjectives are allowed sparingly and consciously, ideally paired with data:

- "Sales quickly grew 450bps from $XX to $YY in Q4."

Always ask whether the reader needs the extra word to reach the same conclusion.
