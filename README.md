# PipelineHQ

**A local-first job-search pipeline that never invents a claim.**

PipelineHQ finds jobs, scores each one against your *verified* experience, and builds a tailored, two-page,
ATS-friendly resume for the good matches. A local AI model does the reading; plain Python enforces the rules, so
nothing on your resume is ever made up. It runs on your own computer, costs nothing, and never applies to jobs on its own.

![PipelineHQ dashboard](docs/dashboard.png)
*Dashboard with sample data: pipeline funnel, match scores, recent runs, sources and best-matching employers.*

---

## Why it exists

AI resume tools are good at writing, and that is the problem: they happily "improve" a resume with skills, numbers
and responsibilities the candidate never had. Recruiters notice, and so do interviewers.

PipelineHQ takes the opposite approach. Everything starts from a **Fact Bank**: a file of your real, verified bullets,
skills and proficiency levels. The model may only *choose, order and lightly reword* those facts, and every output is
checked by deterministic Python guards before it reaches a resume.

## What it does

```mermaid
flowchart LR
    A[Find jobs<br/>company portals, LinkedIn,<br/>Indeed, Naukri] --> B[Score each job<br/>local model + Python rules]
    B -->|approved| C[Plan the resume<br/>right variant, best bullets first]
    B -->|needs review| R[You decide<br/>in the control centre]
    R -->|approve| C
    C --> D[Tailor wording<br/>only with verified keywords]
    D --> E[Word + PDF resume<br/>and application tracker]
```

1. **Find jobs** from company career portals (Workday, Greenhouse, Lever, Ashby, SmartRecruiters, SAP SuccessFactors),
   LinkedIn's public job pages (slow and polite, never your account), Indeed and Naukri, plus a watch list of companies.
   Duplicates across sources are merged; internships and off-domain titles are filtered out.
2. **Score** each job: the local model lists the job's requirements and quotes your Fact Bank as evidence; Python
   accepts a requirement as *met* only if the quote exists word for word, and applies the scoring gates.
3. **Choose a resume variant** (e.g. *design engineer* vs *configuration analyst*). Each job is scored for every
   variant you define, and only evidence that can appear on that variant's resume counts.
4. **Draft** the resume: the most relevant bullets move to the front of each role, the whole resume is kept to about
   two pages, and a bullet is reworded only when it can truthfully work in a job keyword you have proven.
5. **Build** the Word and PDF files from your own resume's layout, and track every application.

![Sample resume](docs/sample_resume.png)
*A generated resume for the fictional sample profile.*

## How it stays honest

These are enforced in code (and covered by the tests), not left to the model:

- A requirement is **met** only with a **verbatim quote** from one Fact Bank line; anything else is at most *partial*.
- Skills you rated **novice / working knowledge** never count as fully met, whichever line is quoted.
- A requirement that names a tool (CATIA, ANSYS, …) needs evidence that names that tool.
- "Master's degree required" is not met by a Bachelor's; years-of-experience claims are never auto-proven.
- In-progress certificates are never evidence; removed claims are blocked as forbidden terms.
- Rewrites may not add numbers, tools or acronyms, drop qualifiers ("when consulted", "basic", "supported"),
  change tense, or drop technical terms; any violation falls back to your original wording.
- Every rewrite is then read by a **critic** (a second model) for natural, truthful wording: it can approve it,
  improve it (the improvement passes the same guard again) or send it back to your original wording. A rule-based
  check also reverts clumsy repetition such as "delivering deliverables", even when no critic is available.
- Visa, clearance, language or student-only postings are never auto-approved.

## The control centre

A small web app on your computer (and optionally your phone on home Wi-Fi):

| Page | What you do there |
|---|---|
| Dashboard | Run now, watch progress, see the funnel and charts |
| Approved | Open the job posting and the tailored resume, mark applied |
| Needs review | See exactly why each job was held; approve (resume is built) or archive |
| Leads | Paste the description of a job that came without one; it is scored like any other |
| Applications | Track status, dates and notes |
| Companies | Add any company by its careers link; the job platform is detected and tested automatically |
| Settings | Roles, locations, sources, daily schedule, phone access, notifications |
| Set up | Turn your resumes into a profile, confirming every fact |
| Health check | Everything PipelineHQ needs, with a fix for anything wrong |

## Quick start (macOS)

Requirements: Python 3.11+, [LM Studio](https://lmstudio.ai) (free) with a model such as **Qwen 3.5 9B**,
and optionally [LibreOffice](https://www.libreoffice.org) (free) for silent PDF conversion.

**Hardware:** a computer with **16 GB of memory** (e.g. an Apple-silicon Mac mini or MacBook) runs a 9B model at about
15-20 words per second; one job takes roughly 1-2 minutes to analyse. 8 GB machines can try a smaller model with weaker
results. Keep 20 GB of disk free for the model and files.

```bash
git clone https://github.com/<you>/PipelineHQ.git && cd PipelineHQ
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                 # add any free cloud keys you have (all optional)
python -m unittest discover tests    # offline tests, a few seconds
python webapp/app.py                 # then open http://localhost:8765
```

It starts with the fictional **example** profile, so you can explore before adding your own data.
In LM Studio: load the model with **Context Length 16384**, start the local server, and turn the model's
"thinking" off (for Qwen 3.5, add `{%- set enable_thinking = false %}` as the first line of its prompt template).
Check it with `python modules/local_analyzer.py --probe`.

### Windows and Linux

Everything except two conveniences works the same: the pipeline, the control centre, resumes and the guided setup.
Not available outside macOS: the **morning run** and **background control centre** buttons (they use macOS's launchd).
Instead, schedule `python orchestrator.py` with Task Scheduler (Windows) or cron (Linux), and start the control centre
with `python webapp/app.py`. Use `venv\Scripts\activate` instead of `source venv/bin/activate` on Windows.
LibreOffice is recommended for PDFs on every system.

## Make it yours: setting up your profile from your resumes

The guided setup turns your existing resumes into a profile in about 20 minutes. The local model only *proposes*;
you confirm every fact, and nothing is saved until you do.

### Before you start

1. **Gather your resumes.** Every version you have: the general one, role-specific ones, older ones. Each version
   may hold bullets the others lack, and the setup merges them without duplicates.
   - Best format: **Word (.docx)**. Text-based **PDF** and **.txt** also work.
   - Scanned PDFs (a photo of a page) cannot be read. If you can't select the text in your PDF viewer, export the
     resume to Word or copy its text into a .txt file first.
2. **Start the local model.** Open LM Studio, load your model (Context Length 16384, thinking off; see Quick start),
   and start the server. Check it on the **Health check** page with **Check including a model test**: it should be
   green before you continue.
3. **Open the control centre**: `python webapp/app.py`, then http://localhost:8765. On the Dashboard press
   **Set up my profile** (also in Settings: *Set up a new profile from resumes*).

### Step 1: Start

Enter your name, e-mail, phone, location (e.g. *Pune, India*) and LinkedIn address: they become your resume header.
Choose a **short profile name**, one word such as *priya*: your profile is saved in `profiles/priya/`.

### Step 2: Your resumes

Press **Choose files**, select all your resumes at once, and press **Add**. Then press **Read my resumes**.
The model reads each one in the background, about a minute per resume; the page updates by itself and moves on
when it's done. You can close the page and come back: progress is saved.

### Step 3: Review your facts (the important step)

Everything the model found is listed, grouped by role. Work through it top to bottom:

- **Roles:** check the job title, employer, location and dates. Dates are written `YYYY-MM` (e.g. `2020-03`) and
  the current job ends with `Present`. A role marked *dates not found in resume* needs a careful look. Untick
  **Keep this role** for anything you don't want on any resume.
- **Bullets:** two kinds of tag:
  - **in your resume** (green): found word for word in your resume. Pre-ticked.
  - **reworded** (amber): the model changed the wording, so it starts **unticked**. Compare it with your resume;
    tick it only if every word is true, or fix the text first.

  Edit any bullet freely, and add missing ones (one per line) in *Add bullets the resumes missed*. Only ticked
  bullets are saved.
- **Skills:** for each skill choose an honest level. *Expert* can count as a full match for a job; *working
  knowledge*, *familiar* and *novice* never do, so you're never presented as stronger than you are. Choose a
  section (*Tools & Software*, *Methods*, *Domain*); sections become the skill lines on your resume.
- **Education and certificates:** mark unfinished certificates **in progress**. They appear on the resume as
  *in progress* but never count as evidence for a job.
- **Languages and additional:** projects, volunteering or awards you want available.

Press **Save and continue**. You need at least one role with one bullet.

### Step 4: Resume variants

A variant is one version of your resume for one kind of job. Most people need one; add a second if you apply for two
different kinds of role (e.g. *Mechanical Design Engineer* and *Configuration Analyst*). For each variant:

- **Name** and **typical job titles** (used to pick the right variant for each job).
- **Headline** under your name, e.g. *Mechanical Design Engineer | Sheet Metal | Siemens NX*.
- **Profile summary**: 2-3 sentences in your own words. Only true claims: this text is printed as written.
- **Roles on this resume**: untick roles that don't belong on this variant.

Then choose the **starter rules for your field**: *mechanical* (mechanical, aerospace, manufacturing), *software*
(software, IT, data) or *general* (anything else).

### Step 5: Search and layout

- **Job titles to search for**, one per line; **locations**, one per line; **country**.
- **Resume layout (optional):** upload a .docx resume whose look you like. Only its page setup, fonts and bullet
  style are used, never its text. Without one, a clean default layout is used.

Press **Create my profile**. Your choices are checked (every bullet, role and variant must fit together); if
something is wrong you'll see what, on the same page.

### Step 6: Done

Press **Use this profile now**. PipelineHQ switches to it immediately (the choice is stored in
`config.local.toml`, which is never published).

### After setup

1. **Health check** once more: the *Profile* line should now show your name and fact count.
2. On the Dashboard press **Run now**. The first run is a full search and takes a while (see *Daily use*).
3. Open **Approved** and read one generated resume next to the job posting. If something looks off, fix the fact
   at its source (below) rather than in the Word file: every future resume then benefits.
4. Review **Needs review** for jobs held back for a reason (a tool you rated novice, a degree, a clearance...).

### Changing your profile later

- Small edits: open the files in `profiles/<yourname>/` in any text editor (table below), save, and restart the
  control centre. A change to `fact_bank.json` makes the next run re-analyse jobs against the new facts.
- Start over or add a second person: run the guided setup again with a new short profile name.
- Your source resumes are kept in `profiles/<yourname>/source_resumes/`, on your computer only.

### If something goes wrong

| What you see | What to do |
|---|---|
| *Could not read the resumes: the local model is not available* | Open LM Studio, load the model, start the server, then press **Read my resumes** again |
| Reading takes many minutes per resume | The model is probably "thinking": run the Health check's model test and follow its fix |
| Most bullets are marked **reworded** | Your model is paraphrasing; try a different model (Qwen 3.5 9B copies faithfully), or tick the ones that are accurate |
| Nothing found in a PDF | It is probably a scanned image: export the resume to Word and add the .docx instead |
| *Not saved yet: ...* after Create my profile | The message names the problem (e.g. a variant with no roles); go back one step with the step buttons and fix it |
| Setup interrupted | Open **Set up** again and press **Continue**: your progress is kept |

### Setting up by hand (advanced)

Everything personal lives in one folder, `profiles/<yourname>/`, which is never committed:

| File | What it holds |
|---|---|
| `fact_bank.json` | Your roles, verified bullets, skills (with proficiency), education, certificates |
| `resume_profile.json` | Resume variants: which roles, bullets and skills each uses, headline, summary |
| `domain.toml` | Profession rules: synonyms, tool names, title words to exclude, blockers, watch list |
| `search.toml` | Roles, locations, sources, schedule (edited from Settings) |
| `resume_template.docx` | Your resume's layout: `python modules/resume_builder.py --make-template your_resume.docx` |

Copy `profiles/example/` to `profiles/<yourname>/`, replace the content, and select it in `config.local.toml`:

```toml
[pipeline]
profile = "yourname"
```

**Health check.** The Dashboard shows a health line; the **Health check** page (or `python modules/doctor.py --model`)
verifies Python packages, your profile, LM Studio (server, loaded model, context length, thinking switched off), the
PDF converter, cloud keys, disk space and the morning run, with a plain fix for anything wrong.

## Daily use

- **Run everything:** press **Run now** on the dashboard, or `python orchestrator.py`.
- **Every morning:** switch on the daily run in Settings (uses macOS's built-in scheduler; the Mac runs it on wake
  if it was asleep).
- **From your phone:** in Settings, run the control centre in the background, set a passcode, then open the shown
  address in Safari and *Add to Home Screen*. Optional push notifications via the free *ntfy* app.

## Costs and privacy

Free to run. The heavy work uses a local model; the optional cloud polish uses free tiers (Gemini, Groq, Cerebras,
NVIDIA, OpenRouter, Mistral) and only ever sees individual bullets plus the job title. Your Fact Bank, resumes, job
data and keys stay on your computer. `scripts/privacy_check.py` checks every file before you publish anything.

## Limits and responsibility

- Scraping job boards may conflict with their terms of service; you run it at your own discretion. LinkedIn is read
  only through public job pages, at a deliberately slow pace, with a small request budget per run.
- Some career platforms (e.g. Avature, Oracle, Taleo) are not supported yet; such companies are searched on the job
  boards instead. Naukri often hides descriptions behind a captcha; those jobs appear as leads to paste.
- PipelineHQ never submits applications. You review and apply yourself.

## Project layout

```
orchestrator.py          one command for the whole chain
modules/scraper.py       Phase 1: portals, job boards, LinkedIn fetcher, dedupe, watch list
modules/local_analyzer.py  Phase 2-3: model analysis, Python guards, scoring, variants
modules/cloud_generator.py Phase 4: resume plan and guarded tailoring
modules/resume_builder.py  Phase 5: Word/PDF resumes and the application ledger
modules/companies.py     add companies, detect their job platform
modules/onboarding.py    guided setup: resumes -> verified, confirmed profile
modules/doctor.py        health check with plain-English fixes
modules/scheduler.py     daily run and background control centre (macOS launchd)
webapp/                  the control centre
profiles/example/        fictional sample profile
profiles/_packs/         starter rules for mechanical, software and general profiles
tests/                   offline tests
```

## Roadmap

- Connector for Avature career sites
- Recruiter / hiring-manager outreach drafts grounded in the Fact Bank
- Interview preparation (STAR answers) per approved job, from the evidence map

## Author

Built by **Mohammed Abul Faise**, mechanical design engineer: [LinkedIn](https://www.linkedin.com/in/mohammed-abul-faise).

Released under the [MIT licence](LICENSE).
