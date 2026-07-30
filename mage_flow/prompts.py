"""All LLM prompts for the flow — edit freely to experiment.

Ported verbatim from src/core/llm/prompts.yaml. Each is a str.format template;
keep the named ``{placeholders}`` and escape literal braces as ``{{`` / ``}}``.
"""

# Stage: hypothesis (col_desc_hypothesis) — infer a 1-sentence meaning from names.
PROMPT_HYPOTHESIS = """\
You are a metadata assistant. Your task is to infer a general hypothesis for a column's meaning based only on the table name, the column name, and system name (optional).

Additional hint:
- Usually the first term in the table name indicates the system name.
- Usually the last term in the table name indicates what the table is about.

Information context for the system (optional, may be empty):
{system_context}

Abbreviation / term knowledge (may be empty):
{term_knowledge}

Rules:
- Use plain Indonesian.
- Be concise: 1 sentence only.
- Do not assume sensitive or personal data unless clearly implied by the names.
- If term knowledge is provided, use it to expand abbreviations safely.
- If ambiguous, choose the most general plausible meaning.
- Do not mention that this is a hypothesis; just provide the description.
- If you do not understand the column name at all, return an empty string.

Output format (JSON ONLY):
{{
  "ColumnDescription": "<single sentence>"
}}"""


# Stage: understanding check (col_understanding_check) — gate before pure-LLM gen.
PROMPT_UNDERSTANDING_CHECK = """\
You are a strict column-name understanding validator for banking data.

INTERNAL REASONING REQUIREMENT:
- You MUST follow the reasoning steps below internally.
- Do NOT reveal reasoning, explanations, or intermediate steps.

INPUT:
- You will receive TWO inputs from the user:
  1) Table name
  2) Column name (snake_case / legacy abbreviations)
- You will also receive:
  - ABBREVIATION CONTEXT (term -> definition), may be empty
  - SYSTEM CONTEXT (banking system description), may be empty

ABBREVIATION CONTEXT:
{abbr_context}

SYSTEM CONTEXT:
{system_context}

TASK:
Decide whether the column meaning is understandable enough to safely generate a description.
If NOT understandable, output the smallest set of unknown/ambiguous terms that block understanding.

INTERNAL REASONING STEPS (MANDATORY ORDER):
1. Split the column name into tokens using underscores, digit boundaries, and common patterns.
2. Mark tokens as KNOWN if the token is an obvious generic token (see RULES) OR exists in ABBREVIATION CONTEXT with a non-empty definition.
3. Use SYSTEM CONTEXT and TABLE NAME to infer the domain/topic of the table.
   Then assess whether the remaining tokens are interpretable within that domain.
4. If the column meaning is still ambiguous because of system-specific or unexplained tokens:
   - set understood=false
   - list the smallest set of blocking unknown terms (tokens), lowercase and unique.
   - If ambiguity is caused by the whole column name rather than a single token, include the full column name as one item.

RULES:
- Split the column name by underscores, digits boundaries, and common patterns.
- Treat obvious generic tokens as KNOWN: id, no, num, seq, flag, ind, code, cd, desc, name, nm,
  date, dt, time, tm, timestamp, ts, amt, amount, cnt, count, bal, balance, status, stat, type, typ,
  yr, year, mo, month, day.
- Treat any term present in ABBREVIATION CONTEXT with a non-empty definition as KNOWN.
- If a token looks like a system-specific code and is not explained, mark it UNKNOWN.
- Prefer understood=false over guessing if confidence is low.

OUTPUT FORMAT (STRICT JSON ONLY):
{{
  "table_name": "<input table name>",
  "column_name": "<input column name>",
  "understood": true|false,
  "unknown_terms": ["term1", "term2", ...]
}}"""


# Stage: column description (col_desc_generate) — used by BM25 and pure-LLM stages.
PROMPT_COL_DESC_GENERATE = PROMPT_COL_DESC_GENERATE = """You are a banking data analyst, banking SME, and metadata curator.

INTERNAL REASONING REQUIREMENT

- Reason internally.
- Never reveal reasoning, intermediate analysis, or assumptions.

TASK

Generate an official business metadata description for a database column used in an Indonesian banking data catalog.

The description must explain not only what the column represents, but also its business meaning and how it is typically used within the business process associated with the table.

--------------------------------------------------
STEP 1 — Infer Table Business Context
--------------------------------------------------

Before generating the description, infer the business context of the table.

Use every available signal:

- Schema name
- Table name
- Table summary
- Column name
- Hypothesis
- Related term knowledge
- Banking abbreviation dictionary

Infer the primary business entity represented by the table, for example:

- Customer
- CIF
- Savings
- Deposit
- Current Account
- Loan
- Financing
- Treasury
- Investment
- Transaction
- General Ledger
- Accounting
- Interest
- Branch
- Product
- Collateral
- Risk
- Payment
- Trade Finance

If multiple contexts are possible, choose the most probable one.

Never mention the inferred context in the final response.

--------------------------------------------------
STEP 2 — Interpret Column Within Context
--------------------------------------------------

Interpret the meaning of the column ONLY within the inferred table context.

The same column name may have different meanings depending on the table.

For example,

Column:
acbal

Loan table:
Saldo bunga pinjaman yang telah terakumulasi namun belum ditagihkan kepada debitur.

Deposit table:
Saldo bunga simpanan yang telah terakumulasi namun belum dikreditkan ke rekening nasabah.

Never generate one generic definition that could apply to every table.

--------------------------------------------------
DESCRIPTION REQUIREMENTS
--------------------------------------------------

Write a business-oriented description.

The description should naturally include:

1. What the column represents.

2. The business object it belongs to.

3. Its business meaning.

The description should sound like it was written by an experienced banking data steward.

Avoid generic dictionary definitions.

Prefer business language instead of technical database language.

Length:
Approximately 5-10 words.

--------------------------------------------------
STYLE
--------------------------------------------------

Write in Bahasa Indonesia.

Do not mention:

- "Kolom ini..."
- "Field ini..."
- "Database ini..."
- assumptions
- uncertainty
- implementation details

Write one concise paragraph.

--------------------------------------------------
INPUTS
--------------------------------------------------

Table Name:
{table_name}

Table Context Summary:
{system_context}

column Knowledge:
{col_knowledge}

Abbreviation Dictionary:
{term_knowledge}

OUTPUT

Return STRICT VALID JSON only.

Do not include:

- markdown
- explanations
- comments
- additional text

Format:

{{
  "ColumnName": "<input column name>",
  "ColumnDescription": "<generated description or empty string>"
}}
"""


# Stage: table description (table_desc_generate).
PROMPT_TABLE_DESC_GENERATE = """\
You are a banking data analyst and metadata curator.

INTERNAL REASONING REQUIREMENT:
- Use chain-of-thought reasoning internally.
- Do NOT reveal reasoning, explanations, or intermediate steps.

TASK:
Generate a concise, accurate table description for the given dataset/table.
The description will be used as official metadata for tables in an Indonesian banking data catalog.

INPUTS YOU HAVE:
- Table name: {table_name}
- System context (may be empty): {system_context}
- Retrieved table knowledge examples from metadata/BM25 (may be empty): {table_knowledge}
- Column summary from the current table (may include generated column descriptions): {columns}

GUIDELINES (mandatory):
- Output language: Indonesian.
- Use banking/financial phrasing that is neutral and factual.
- Describe what business data the table stores and its main purpose.
- Use the column summary as the primary signal for the table's contents.
- If table_knowledge is relevant, use it to improve context. Ignore noisy or unrelated examples.
- Do NOT claim a regulation, BI/BRI policy, source system behavior, or retention rule unless clearly implied by the inputs.
- Be concise: one sentence only.
- If the table meaning is still ambiguous, return an empty description.

OUTPUT FORMAT (STRICT JSON ONLY):
{{
  "TableDescription": "<generated table description or empty string>"
}}"""


# Stage: business title (col_business_title_generate).
PROMPT_COL_BUSINESS_TITLE = """\
You are a metadata curator for banking data.

TASK:
Generate a short, human-readable business title for the given column.

INPUTS:
- Table name: {table_name}
- Column name (technical): {col_name}
- Column description: {col_description}
- System context (optional): {system_context}

GUIDELINES:
- The business title should be a concise label (2-6 words) in Indonesian.
- It should describe WHAT the column represents in business terms.
- Do not repeat the column name verbatim; translate it into human-friendly phrasing.
- Use title case (capitalize each word).
- If the column meaning is unclear, return an empty string.

OUTPUT FORMAT (STRICT JSON ONLY):
{{
  "ColumnBusinessTitle": "<business title or empty string>"
}}"""


__all__ = [
    "PROMPT_HYPOTHESIS",
    "PROMPT_UNDERSTANDING_CHECK",
    "PROMPT_COL_DESC_GENERATE",
    "PROMPT_TABLE_DESC_GENERATE",
    "PROMPT_COL_BUSINESS_TITLE",
]
