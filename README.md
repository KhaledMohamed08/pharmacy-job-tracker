# Pharmacy Job Tracker 💊

**Dashboard:** https://khaledmohamed08.github.io/pharmacy-job-tracker/

Once a day it checks **pharma companies**, **clinical research companies (CROs)**, **UN agencies and NGOs** and **remote job boards**. It scores each job for a hospital pharmacist in Greater Cairo with about 2–3 years of experience, a native Arabic speaker with intermediate English and an Egyptian Pharmacists Syndicate licence. Then it:

- updates a **phone-friendly dashboard** you can add to your home screen
- sends a **free Telegram message** (or email) with the new best matches

**What it looks for, in order of priority:**

1. Hospital and clinical pharmacist jobs
2. Office-based or remote jobs at pharma companies, CROs and NGOs: pharmacovigilance and drug safety, regulatory affairs, medical information, quality (QA/QC), and supply chain and procurement

**Where:** on-site and hybrid jobs only in Greater Cairo (Cairo, Giza, New Cairo, 6th of October, Sheikh Zayed, Nasr City, Maadi and nearby). Jobs in other Egyptian cities are hidden. Remote jobs from anywhere appear only if they're open to applicants in Egypt. A job that says "Egypt" without a city shows a warning to check it's in Greater Cairo.

**Always hidden:** retail and community pharmacy (chains and independents), medical reps and other field, sales or travel-based roles, commercial roles (marketing, market access, brand, business development, medical science liaison, customer experience), pharmacy technician and assistant roles, and jobs that need a licence from another country (DHA, DOH, SCFHS, Prometric, US, UK and others).

**Scoring:** Arabic as a plus raises the score. Asking for 5 or more years of experience, senior roles and native-level English writing (such as medical writing) lower it, but those jobs aren't hidden.

It runs for free: GitHub runs the search and hosts the page, and Telegram bots cost nothing.

## Sources

| Source | What it covers |
|---|---|
| GSK, Pfizer, Novartis, AstraZeneca, Sanofi, Johnson & Johnson, Abbott, MSD | Their own careers sites, searched for Egypt |
| IQVIA, Thermo Fisher (PPD), ICON | Clinical research companies, searched for Egypt, pharmacovigilance, drug safety and medical information |
| Ergomed (PrimeVigilance), AbbVie, Veeva | Drug safety, pharma and life-science software jobs |
| UN Careers, UNHCR, WFP, UNDP, ICRC | Their own career sites. UNICEF's site blocks automated access, so its jobs come through Impactpool |
| Impactpool | Jobs from WHO and many other UN agencies and NGOs, searched for pharmacy and medicines roles |
| Himalayas, Remotive, RemoteOK, Working Nomads, Jobicy | Remote job boards |

AI-training gigs, such as "pharmacist AI trainer", are filtered out. Wuzzuf, Bayt and LinkedIn block automated access and aren't searched, so set up their own free job alerts. Most Cairo hospital jobs are posted there.

To add a pharma company or CRO that uses Workday (its careers address contains `myworkdayjobs.com`), add a line to `PHARMA_WORKDAY` or `CRO_WORKDAY` in `fetch_jobs.py`.

## Using the dashboard

- **Lists:** All jobs, Pharmacy roles, Cairo & Egypt, Remote and My list.
- **Narrow down:** combine Arabic a plus, UN & NGOs, Pharma & CROs, Closing this week, Up to 3 years and New with a practice area: Hospital & clinical, Drug safety, Regulatory, Medical information, Quality, or Supply & procurement.
- **Match label:** every job shows Strong, Good or Possible match, based on a score out of 100. Tap a job to see why it fits and any warnings.
- **Key facts:** the qualifications it asks for (PharmD, B.Pharm, pharmacist licence, GCP), experience, contract type and pay appear when the posting states them.
- **Bookmark:** tap it on a job to save it to My list.
- **Track this job:** in the job details, mark it Saved, Applied, Interview or Offer, or choose *Not for me* to hide it.
- **Settings and sources:** choose light or dark mode, copy your list to another phone, and see which sources are working.

Your saved jobs are stored on each device. To copy them, open **Settings and sources**, tap **Copy code**, send the code to the other phone, paste it there and tap **Import code**.

### Apply directly
Open **Apply directly** for Cairo hospitals, pharma companies, CROs, UN employers, free certificates (such as Good Clinical Practice) and job alerts. Each one has a tip. Mark each one To do, Started, Applied or Accepted, and add notes. Three weeks after you mark one Applied, it suggests a follow-up.

The list is in `docs/direct.json`, so places can be added or edited there. The daily run checks these links. If one fails two days in a row, the first Telegram chat (or `ADMIN_CHAT_ID`) gets a message and the card shows a warning. Sites that block automated checks are skipped rather than reported.

### Apply kit
Open **Apply kit** and fill in **My details** once: degree, licence, experience, areas you want to work in and skills. Then, in any job's details:

- **Email your application:** appears when the posting gives an application email. It opens an email that's ready to review, with the CV still to attach.
- **Write a cover letter:** creates an English or Arabic letter to edit and copy.
- **Copy a prompt for an AI assistant:** copies the job and your experience, without your email or phone, to paste into an assistant.

**My answers** has starter answers for common interview questions, such as handling a difficult patient or a medication error. Everything in the Apply kit is saved only in that device's browser, not on GitHub. Clearing browser data deletes it, so use **Backup** and keep the code somewhere private.

### Add it to your phone
- **iPhone (Safari):** Share → **Add to Home Screen**
- **Android (Chrome):** ⋮ → **Add to Home screen**

## How the daily run works

The search is scheduled for 05:00 UTC, which is about 7–8 am in Cairo. GitHub can start scheduled runs hours late when it's busy.

- **New jobs:** Telegram messages you only when there are new jobs scoring 55 or higher, so some days will be quiet. Tapping a job in the message opens its details in the dashboard.
- **No repeats:** each job is remembered for 180 days, so it isn't announced twice if it disappears and comes back.
- **Source problems:** if a source fails, its previous jobs stay on the list until it recovers.
- **Time limit:** the search stops after 12 minutes, so one slow site can't hold up the run. A source that runs out of time counts as failed for that day. Change the limit with the `RUN_MINUTES` environment variable.
- **Problem alerts:** the admin chat gets one Telegram message if the run fails, if a source fails two runs in a row (and again when it recovers), or if a run finds no matches at all.
- **Tests:** automatic tests run before every daily search and after every change. If they fail, the search doesn't run and you get the failure alert.

To run it now, open **Actions → Daily job search → Run workflow** on GitHub.

## First-time setup

1. Create the GitHub repository `KhaledMohamed08/pharmacy-job-tracker` and push this folder to it.
2. Turn on GitHub Pages: **Settings → Pages → Deploy from a branch**, branch `main`, folder `/docs`.
3. Add the secrets below. Each project needs its own Telegram bot and secrets: secrets aren't shared between repositories.
4. Run the workflow once from **Actions → Daily job search → Run workflow**.

## Settings

GitHub secrets are in **Settings → Secrets and variables → Actions**.

| Secret | Used for |
|---|---|
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Telegram alerts. Use `./setup_telegram.sh` |
| `ADMIN_CHAT_ID` | Optional chat for maintenance messages, such as broken links. Without it they go to the first chat in `TELEGRAM_CHAT_ID` |
| `SMTP_USER`, `SMTP_PASSWORD`, `EMAIL_TO` | Optional email alerts |

### Change who gets Telegram alerts
Create a bot with @BotFather. Everyone who should get alerts sends the bot a new message. Then run this from the project folder and paste the bot token:

```bash
./setup_telegram.sh
```

The script replaces the saved list using recent messages, so include everyone each time. It's set up for the `KhaledMohamed08/pharmacy-job-tracker` repository.

### Email alerts (optional)
Create a Gmail **App password** (Google Account → Security → 2-Step Verification → App passwords), then add `SMTP_USER` (your Gmail address), `SMTP_PASSWORD` (the app password) and `EMAIL_TO` (one or more addresses, separated by commas).

### Tune the matching
Edit `profile.json`:

- `min_score`: lower it to see more jobs, raise it to see fewer
- `notify_min_score`: how good a job must be to trigger an alert
- `home_area`, `other_egypt_cities`: where on-site and hybrid jobs may be
- `role_keywords`: job titles that count as pharmacy roles
- `conditional_role_keywords`, `pharma_context`: quality and supply titles that count only at pharma companies, CROs and NGOs, or when the posting is about medicines
- `role_text`: description words that show a job wants a pharmacy background
- `areas`: practice areas and how much each one is worth. Their names become the dashboard filters
- `hide_titles`, `hide_orgs`, `hide_locations`, `hide_text`: jobs that are always hidden, such as retail, sales, commercial, technician, travel and foreign-licence jobs
- `arabic_patterns`, `native_english_patterns`, `other_languages`: language bonuses and penalties
- `exclude_title_or_company`, `exclude_description`, `not_suitable_titles`: other jobs that get filtered out

### Change the message intros
Each alert starts with a different warm, motivating line from `quotes.txt`, one per line. They rotate in order, and none repeats until all have been used. Add, edit or remove lines at any time.

### Run the tests

```bash
python3 -m unittest discover -s tests -v
```

They use made-up data and never contact the job sites.

### Run it on your computer (optional)
It needs Python 3.9 or newer and nothing else:

```bash
python3 fetch_jobs.py
python3 -m http.server -d docs
```

Then open http://localhost:8000. Without the secrets above, alerts are skipped.

## Worth doing outside this tool

- **Egyptian Pharmacists Syndicate registration:** keep your membership and licence up to date, because most employers ask for it.
- **Good Clinical Practice certificate:** free online, and often asked for in drug safety and medical information jobs.
- **Hospital job alerts:** most Cairo hospitals post on Wuzzuf and LinkedIn, so set alerts for "clinical pharmacist" in Cairo and Giza.
- **Hospital experience on your CV:** name the units, systems and medication-safety work you did. It's what makes you stand out for drug safety and medical information jobs.
