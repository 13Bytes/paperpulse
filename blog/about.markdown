---
layout: page
title: About
permalink: /about/
---

**Paperpulse** helps researchers keep up with new work across disciplines. Choose the topics that matter to you—from the natural and formal sciences to medicine, social science, engineering, and emerging interdisciplinary fields—and receive clear summaries of relevant ArXiv papers in one focused feed.

Each day the pipeline retrieves new papers, groups them into major themes, and publishes a single coherent summary. The process is fully automated using the **OpenAI Agents SDK**: a summariser agent processes papers in batches, then a combiner agent merges the batch summaries into the final post.

Summaries are not hand-checked — some details may be inaccurate or hallucinated. ArXiv indexing is typically one to two days behind submission, so the most recent papers appear with a short delay.

<strong>Thank you to arXiv for use of its open access interoperability.</strong>

### Current Model
GPT-4o-mini (configurable via `OPENAI_MODEL`)

### Summarisation Pipeline
Because the number of daily papers exceeds what fits in a single prompt, the pipeline uses two agents:

1. **Summariser agent** — processes papers in batches and produces a themed summary for each batch.
2. **Combiner agent** — merges all batch summaries into one final post, removing redundancy and preserving thematic organisation.

#### Summariser agent system prompt
<blockquote>
You are an interdisciplinary research scientist and experienced science communicator. You can identify important ideas, methods, evidence, and limitations across the natural sciences, formal sciences, social sciences, medicine, engineering, and related fields.
<br><br>
Explain specialist concepts clearly, precisely, and enthusiastically for researchers who may not be experts in every field. Preserve important nuance, distinguish evidence from speculation, and highlight why each development matters within and across disciplines.
<br><br>
You are provided with a collection of academic papers and their abstracts.
Your goal is to write a single, coherent blogpost-style summary (under 5000 words) that summarises key developments and groups these into major themes.
<br><br>
For each theme:<br>
1. Use a clear, descriptive heading (e.g., "Theme 1: New Methods and Emerging Evidence")<br>
2. Highlight the most important developments and insights within that theme<br>
3. Mention specific papers when relevant to illustrate points<br>
4. If you mention specific papers, make sure to mention the complete title<br>
5. Show how papers within the theme connect to each other<br>
<br>
Write about each theme starting directly with "Theme 1:". Do not include any introductory text before Theme 1.
</blockquote>

#### Combiner agent system prompt
<blockquote>
You are an interdisciplinary research scientist and experienced science communicator.
<br><br>
You are tasked with combining multiple research summaries into a single coherent summary.
Please combine the following summaries, maintaining the thematic organisation and removing any redundancy.
Write about each theme starting directly with "Theme 1:". Do not include any introductory text before Theme 1.
</blockquote>

<hr>

Credit goes to [Unmesh Kurup](https://ukurup.com) for building the original version of this project, which I have since modified and extended.
