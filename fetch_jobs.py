#!/usr/bin/env python3
"""Pharmacy Job Tracker: collect pharmacy jobs in Egypt and remote pharma jobs
(pharma companies, CROs, UN agencies, NGOs and remote boards), score them for an
early-career pharmacist, write docs/jobs.json for the dashboard, and send a free
Telegram (or email) digest of new matches.

Standard library only, so it runs anywhere with Python 3.9+ (no pip install).
"""
import concurrent.futures as cf
import html
import json
import os
import re
import smtplib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from email.utils import parsedate_to_datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "docs" / "jobs.json"
OUTBOX = ROOT / "outbox.json"  # alerts waiting to be sent; never committed
PROFILE = json.loads((ROOT / "profile.json").read_text(encoding="utf-8"))
NOW = datetime.now(timezone.utc)
SEEN_DAYS = 180  # how long a removed job is remembered
UA = "Mozilla/5.0 (PharmacyJobTracker personal job search; +https://github.com)"


# ---------------------------------------------------------------- helpers

RUN_LIMIT = float(os.environ.get("RUN_MINUTES", 12)) * 60  # slow sites stop after this
DEADLINE = [None]  # set when the run starts


class OutOfTime(Exception):
    """The run's time limit was reached, so this source stops where it is."""


def http(url, data=None, headers=None, timeout=30, retries=2):
    if DEADLINE[0] and time.time() > DEADLINE[0]:
        raise OutOfTime("run time limit reached")
    hdrs = {"User-Agent": UA, "Accept": "*/*"}
    hdrs.update(headers or {})
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        hdrs["Content-Type"] = "application/json"
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, data=body, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except Exception:
            if attempt == retries:
                raise
            time.sleep(2 * (attempt + 1))


def http_json(url, data=None, **kw):
    return json.loads(http(url, data=data, **kw))


def strip_html(s):
    s = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", s or "", flags=re.S | re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def parse_date(s):
    if not s:
        return None
    if isinstance(s, (int, float)) or (isinstance(s, str) and s.isdigit()):
        return datetime.fromtimestamp(int(s), timezone.utc)
    s = s.strip()
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        pass
    for fmt in ("%d %b %Y", "%d %B %Y", "%Y-%m-%d", "%B %d, %Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    try:
        return parsedate_to_datetime(s).astimezone(timezone.utc)
    except Exception:
        return None


def job(source, org, title, url, location="", text="", remote=False, deadline=None,
        posted=None, level="", intl=False, check_location=True, allowed=None, pharma=False):
    return {
        "source": source, "org": (org or "").strip(), "title": strip_html(title),
        "url": url, "location": (location or "").strip(), "text": strip_html(text),
        "remote": remote, "deadline": parse_date(deadline), "posted": parse_date(posted),
        "level": level or "", "intl": intl, "check_location": check_location,
        "allowed": allowed, "pharma": pharma,
    }


REMOTE_RE = re.compile(
    r"home[- ]based|\bremote(ly)?\b(?! (area|location|communit|region|village|district|part))"
    r"|work(ing)? from home|telecommut|teleworking|virtual(ly)? based", re.I)


NOT_REMOTE_RE = re.compile(
    r"\bno(t)? remote|remote work is not|not (be )?(done |performed )?remote|cannot be (done )?remote"
    r"|in[- ]person only|on[- ]site only", re.I)


def looks_remote(*parts):
    s = " ".join(p or "" for p in parts)
    return bool(REMOTE_RE.search(s)) and not NOT_REMOTE_RE.search(s)


# ---------------------------------------------------------------- sources
# Each returns a list of job dicts. A failing source never stops the others.

def src_un_careers():
    """UN Secretariat (careers.un.org) - official JSON used by the site itself."""
    out, page = [], 0
    while page < 10:
        d = http_json(
            "https://careers.un.org/api/public/opening/jo/list/filteredV2/en",
            {"filterConfig": {"keyword": ""},
             "pagination": {"page": page, "itemPerPage": 100, "sortBy": "startDate", "sortDirection": -1}})
        items = d["data"]["list"]
        for j in items:
            stations = ", ".join(s.get("description", "").title() for s in j.get("dutyStation") or [])
            desc = j.get("jobDescription", "")
            m = re.search(r"Work Location</div><div class='jobPostingItemContent'>(.*?)</div>", desc, re.S)
            work_loc = strip_html(m.group(1)) if m else ""
            out.append(job(
                "UN Careers", (j.get("dept") or {}).get("name", "United Nations").title(),
                j.get("postingTitle") or j.get("jobTitle"),
                f"https://careers.un.org/jobSearchDescription/{j['jobId']}?language=en",
                location=work_loc if looks_remote(work_loc) else stations,
                text=desc, remote=looks_remote(work_loc, j.get("jobTitle", "")),
                deadline=j.get("endDate"), posted=j.get("startDate"),
                level=(j.get("jl") or {}).get("name", ""), intl=True, check_location=False))
        if len(items) < 100:
            break
        page += 1
    return out


WORKDAY = [  # (label, list endpoint, public site base, international organization?)
    ("UNHCR", "https://unhcr.wd3.myworkdayjobs.com/wday/cxs/unhcr/External",
     "https://unhcr.wd3.myworkdayjobs.com/External", True),
    ("WFP", "https://wd3.myworkdaysite.com/wday/cxs/wfp/job_openings",
     "https://wd3.myworkdaysite.com/recruiting/wfp/job_openings", True),
]

# Pharma companies and CROs on Workday. Their lists are huge, so only searches are read.
# Add a company by copying its careers-site address (…myworkdayjobs.com/<tenant>/<site>).
PHARMA_WORKDAY = [  # (label, tenant host, tenant, site)
    ("GSK", "gsk.wd5", "gsk", "GSKCareers"),
    ("Pfizer", "pfizer.wd1", "pfizer", "PfizerCareers"),
    ("Novartis", "novartis.wd3", "novartis", "Novartis_Careers"),
    ("AstraZeneca", "astrazeneca.wd3", "astrazeneca", "Careers"),
    ("Sanofi", "sanofi.wd3", "sanofi", "SanofiCareers"),
    ("Johnson & Johnson", "jj.wd5", "jj", "JJ"),
    ("Abbott", "abbott.wd5", "abbott", "abbottcareers"),
    ("MSD", "msd.wd5", "msd", "SearchJobs"),
    ("Viatris", "viatris.wd5", "viatris", "External"),
]
CRO_WORKDAY = [
    ("IQVIA", "iqvia.wd1", "iqvia", "IQVIA"),
    ("Thermo Fisher (PPD)", "thermofisher.wd5", "thermofisher", "ThermoFisherCareers"),
    ("ICON", "icon.wd3", "icon", "broadbean_external"),
]
PHARMA_SEARCHES = ["Egypt"]
CRO_SEARCHES = ["Egypt", "pharmacovigilance", "drug safety", "medical information", "regulatory affairs"]


def workday_postings(api, search="", limit=600):
    offset, posts = 0, []
    while offset < limit:
        d = http_json(api + "/jobs", {"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": search},
                      timeout=60)
        posts += d.get("jobPostings", [])
        offset += 20
        if offset >= d.get("total", 0):
            break
    return posts


def workday_job(org, api, site, p, intl, pharma=False):
    title, loc = p.get("title", ""), p.get("locationsText", "")
    text, deadline = "", None
    try:
        info = http_json(api + p["externalPath"], timeout=60).get("jobPostingInfo", {})
        text = info.get("jobDescription", "")
        loc = info.get("location", loc)
        deadline = info.get("endDate")
    except OutOfTime:
        raise
    except Exception:
        pass
    return job(org, org, title, site + p["externalPath"], location=loc, text=text,
               remote=looks_remote(title, loc, strip_html(text)[:2000]),
               deadline=deadline, level=" ".join(p.get("bulletFields") or []),
               posted=info_date(p.get("postedOn")), intl=intl, check_location=not intl, pharma=pharma)


def info_date(posted_on):
    """Workday says 'Posted Today' / 'Posted 3 Days Ago' / 'Posted 30+ Days Ago'."""
    m = re.search(r"(\d+)\+? Days? Ago", posted_on or "")
    if m:
        return iso(NOW - timedelta(days=int(m.group(1))))
    return iso(NOW) if re.search(r"Today|Yesterday", posted_on or "") else None


def src_workday():
    """UN agencies: their lists are short, so every posting is checked."""
    out = []
    for org, api, site, intl in WORKDAY:
        posts = workday_postings(api)
        if not posts:
            raise RuntimeError(f"{org}: no postings returned")
        for p in posts:
            # Only open the full description for plausible matches (keeps it fast and polite).
            if plausible(p.get("title", ""), p.get("locationsText", "")):
                out.append(workday_job(org, api, site, p, intl))
    return out


def workday_company(company, searches):
    org, host, tenant, site_name = company
    api = f"https://{host}.myworkdayjobs.com/wday/cxs/{tenant}/{site_name}"
    site = f"https://{host}.myworkdayjobs.com/{site_name}"
    posts, seen, out = [p for q in searches for p in workday_postings(api, q, limit=60)], set(), []
    for p in posts:
        if p.get("externalPath") in seen:
            continue
        seen.add(p.get("externalPath"))
        if plausible(p.get("title", ""), p.get("locationsText", "")):
            out.append(workday_job(org, api, site, p, False, pharma=True))
    return out


def workday_search(companies, searches):
    """Companies are searched side by side, so one slow site doesn't hold up the rest."""
    out, failed = [], []
    with cf.ThreadPoolExecutor(max_workers=len(companies)) as ex:
        futs = {ex.submit(workday_company, c, searches): c[0] for c in companies}
        for f in cf.as_completed(futs):
            try:
                out += f.result()
            except Exception as e:
                failed.append(f"{futs[f]}: {type(e).__name__}")
    if len(failed) == len(companies):
        raise RuntimeError("; ".join(failed)[:200])
    if failed:
        print("Workday: " + "; ".join(failed), file=sys.stderr)
    return out


def src_pharma_companies():
    return workday_search(PHARMA_WORKDAY, PHARMA_SEARCHES)


def src_cros():
    return workday_search(CRO_WORKDAY, CRO_SEARCHES)


def plausible(title, location):
    """Worth opening the full posting? Keeps the number of requests small."""
    return bool(ROLE_TITLE.search(title) or HOME.search(location) or looks_remote(title, location))


def src_impactpool():
    """Impactpool lists jobs from nearly every UN agency, the EU and development banks."""
    cards = {}
    for q in ["pharmacist", "pharmacy", "pharmaceutical", "medicines", "health products",
              "pharmacovigilance", "Egypt", "Cairo",
              # Organizations whose own sites block or can't be read by this tool
              "WHO", "IFRC", "MSF"]:
        for page in (1, 2, 3):
            body = http("https://www.impactpool.org/search?" + urllib.parse.urlencode({"q": q, "page": page}))
            found = re.findall(r"<div class='job'>(.*?)</a></div>", body, re.S)
            for c in found:
                m = re.search(r'href="(/jobs/\d+)"', c)
                t = re.search(r"type='cardTitle'>(.*?)</h3>", c, re.S)
                bits = [strip_html(b) for b in re.findall(r"type='bodyEmphasis'>(.*?)</div>", c, re.S)]
                if m and t:
                    cards[m.group(1)] = (strip_html(t.group(1)), bits)
            if len(found) < 40:
                break
            time.sleep(0.5)
    if not cards:
        raise RuntimeError("no cards found")
    out = []
    for path, (title, bits) in cards.items():
        org, loc = (bits + ["", ""])[:2]
        if not (HOME.search(loc) or looks_remote(title, loc)):
            continue  # outside Egypt and not remote
        url = "https://www.impactpool.org" + path
        text, deadline = "", None
        try:
            text = strip_html(http(url))
            d = re.search(r"Application deadline:\s*(\w+ \d{1,2}, \d{4})", text)
            deadline = d.group(1) if d else None
            text = text[text.find(title, 200):]
        except Exception:
            pass
        out.append(job("Impactpool", org, title, url, location=loc, text=text,
                       remote=looks_remote(title, loc), deadline=deadline,
                       level=" ".join(bits[2:]), intl=True, check_location=False))
        time.sleep(0.5)
    return out


def oracle_jobs(org, host, site, public):
    api = host + "/hcmRestApi/resources/latest/"
    d = http_json(api + "recruitingCEJobRequisitions?onlyData=true&expand=requisitionList"
                  f"&finder=findReqs;siteNumber={site},limit=200,sortBy=POSTING_DATES_DESC")
    reqs = d["items"][0]["requisitionList"]
    if not reqs:
        raise RuntimeError("no jobs returned")
    out = []
    for r in reqs:
        title, loc = r.get("Title", ""), r.get("PrimaryLocation") or ""
        if not plausible(title, loc):
            continue
        info = http_json(api + "recruitingCEJobRequisitionDetails?expand=all&onlyData=true"
                         f"&finder=ById;Id=%22{r['Id']}%22,siteNumber={site}")["items"][0]
        text = info.get("ExternalDescriptionStr", "")
        out.append(job(org, org, title, f"{public}/job/{r['Id']}",
                       location=loc, text=text, remote=looks_remote(title, loc),
                       deadline=info.get("ExternalPostedEndDate"), posted=r.get("PostedDate"),
                       level=info.get("RequisitionType", ""), intl=True, check_location=False))
    return out


def src_undp():
    return oracle_jobs("UNDP", "https://estm.fa.em2.oraclecloud.com", "CX_1",
                       "https://estm.fa.em2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1")


def src_successfactors(org, base, query="", intl=True):
    """Search pages only: robots.txt disallows the RSS feed on these sites."""
    out, seen = [], set()
    for row in range(0, 200, 25):
        body = http(f"{base}/search/?" + urllib.parse.urlencode({"q": query, "startrow": row}))
        rows = re.split(r'class="data-row|class="job-tile job-id-', body)[1:]
        for r in rows:
            m = re.search(r'href="(/job/[^"]+)"[^>]*>(.*?)</a>', r, re.S)
            if not m or m.group(1) in seen:
                continue
            seen.add(m.group(1))
            title = strip_html(m.group(2))
            place = re.search(r'class="jobLocation[^"]*">(.*?)</span>', r, re.S) \
                or re.search(r'class="section-field location[^"]*"[^>]*>(.*?)</div>', r, re.S) \
                or re.search(r'Country\s*</span>\s*<[^>]*>(.*?)<', r, re.S)
            loc = re.sub(r"^Location\s*", "", strip_html(place.group(1))) if place else ""
            if not plausible(title, loc):
                continue
            text = strip_html(http(base + m.group(1)))
            d = re.search(r"(?:deadline|closing date)[^:]{0,50}:\s*(\d{2}/\d{2}/\d{4}|\d{1,2} \w+ \d{4})", text, re.I)
            out.append(job(org, org, title, base + m.group(1), location=loc, text=text,
                           remote=looks_remote(title, loc), deadline=d.group(1) if d else None,
                           intl=intl, check_location=not intl, pharma=not intl))
        if len(rows) < 25:
            break
    if not seen and not query:
        raise RuntimeError("no rows found")
    return out


def src_boehringer():
    return src_successfactors("Boehringer Ingelheim", "https://jobs.boehringer-ingelheim.com", "Egypt", intl=False)


def src_merck_kgaa():
    """Merck KGaA (Phenom careers site): the search results are embedded in the page."""
    out = []
    for q in ("Egypt", "Cairo"):
        body = http("https://careers.merckgroup.com/global/en/search-results?" + urllib.parse.urlencode({"keywords": q}))
        i = body.find('"eagerLoadRefineSearch"')
        if i < 0:
            raise RuntimeError("search results not found on the page")
        data, _ = json.JSONDecoder().raw_decode(body, body.index("{", i))
        for j in (data.get("data") or {}).get("jobs", []):
            loc = j.get("cityStateCountry") or j.get("location") or ""
            out.append(job("Merck KGaA", "Merck KGaA", j.get("title"),
                           f"https://careers.merckgroup.com/global/en/job/{j.get('jobId')}",
                           location=loc, text=j.get("descriptionTeaser", ""),
                           remote=looks_remote(j.get("title"), loc), posted=j.get("postedDate"), pharma=True))
    return out


def src_iom():
    return oracle_jobs("IOM", "https://fa-evlj-saasfaprod1.fa.ocs.oraclecloud.com", "CX_1001",
                       "https://fa-evlj-saasfaprod1.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001")


def src_icrc():
    return src_successfactors("ICRC", "https://careers.icrc.org")


# Pharma, CRO and health-tech companies with public job boards (add more names here).
SMARTRECRUITERS = ["Ergomed", "AbbVie"]   # Ergomed runs PrimeVigilance (drug safety)
LEVER = ["veeva"]                         # life-science software, many pharmacovigilance roles


def src_health_companies():
    out = []
    for c in SMARTRECRUITERS:
        for params in ({"country": "eg"}, {"q": "remote"}, {"q": "pharmacovigilance"}):
            d = http_json(f"https://api.smartrecruiters.com/v1/companies/{c}/postings?"
                          + urllib.parse.urlencode({"limit": 100, **params}))
            for j in d.get("content", []):
                l = j.get("location") or {}
                out.append(job("Health companies", j.get("company", {}).get("name") or c, j.get("name"),
                               f"https://jobs.smartrecruiters.com/{c}/{j['id']}",
                               location=l.get("fullLocation") or l.get("city"),
                               remote=bool(l.get("remote")), posted=j.get("releasedDate"), pharma=True))
    for c in LEVER:
        for j in http_json(f"https://api.lever.co/v0/postings/{c}?mode=json"):
            cat = j.get("categories") or {}
            loc = cat.get("location") or ""
            out.append(job("Health companies", c.title(), j.get("text"), j.get("hostedUrl"),
                           location=loc, text=j.get("descriptionPlain", ""),
                           remote=j.get("workplaceType") == "remote" or looks_remote(j.get("text"), loc),
                           posted=j.get("createdAt", 0) // 1000 or None, pharma=True))
    return out


class FeedSkipped(Exception):
    """Source deliberately not fetched this run: don't retry, keep its previous jobs."""
    def __init__(self, message, ok=True):
        super().__init__(message)
        self.ok = ok


def src_himalayas():
    out, seen = [], set()
    for q in ["pharmacist", "pharmacy", "pharmacovigilance", "drug safety", "medical writer",
              "regulatory affairs", "clinical research", "medical information"]:
        for offset in (0, 20, 40):
            d = http_json("https://himalayas.app/jobs/api/search?" + urllib.parse.urlencode({"q": q, "offset": offset}))
            for j in d.get("jobs", []):
                if j["guid"] in seen:
                    continue
                seen.add(j["guid"])
                restr = j.get("locationRestrictions") or []
                out.append(job("Himalayas", j.get("companyName"), j.get("title"), j.get("applicationLink"),
                               location=", ".join(restr) or "Worldwide", text=j.get("description", ""),
                               remote=True, deadline=j.get("expiryDate"), posted=j.get("pubDate"),
                               level=" ".join(j.get("seniority") or []), allowed=restr))
            if len(d.get("jobs", [])) < 20:
                break
    return out


def src_remotive():
    d = http_json("https://remotive.com/api/remote-jobs")
    return [job("Remotive", j.get("company_name"), j.get("title"), j.get("url"),
                location=j.get("candidate_required_location"), text=j.get("description"),
                remote=True, posted=j.get("publication_date")) for j in d.get("jobs", [])]


def src_remoteok():
    d = http_json("https://remoteok.com/api")
    return [job("RemoteOK", j.get("company"), j.get("position"), j.get("url"),
                location=j.get("location") or "Worldwide",
                text=j.get("description", "") + " " + " ".join(j.get("tags") or []),
                remote=True, posted=j.get("date")) for j in d[1:] if isinstance(j, dict)]


def src_workingnomads():
    d = http_json("https://www.workingnomads.com/api/exposed_jobs/")
    return [job("Working Nomads", j.get("company_name"), j.get("title"), j.get("url"),
                location=j.get("location"), text=j.get("description", "") + " " + (j.get("tags") or ""),
                remote=True, posted=j.get("pub_date")) for j in d]


def src_jobicy():
    d = http_json("https://jobicy.com/api/v2/remote-jobs?count=100")
    return [job("Jobicy", j.get("companyName"), j.get("jobTitle"), j.get("url"),
                location=j.get("jobGeo"), text=j.get("jobDescription") or j.get("jobExcerpt"),
                remote=True, posted=j.get("pubDate")) for j in d.get("jobs", [])]


# These only return plausible matches, so 0 can be normal (they raise if the site itself fails).
PREFILTERED = {"UNHCR, WFP", "Pharma companies", "CROs", "UNDP", "ICRC", "IOM", "Impactpool",
               "Boehringer Ingelheim", "Merck KGaA"}
SOURCES = {
    "UN Careers": src_un_careers,
    "UNHCR, WFP": src_workday, "Pharma companies": src_pharma_companies, "CROs": src_cros,
    "Impactpool": src_impactpool, "UNDP": src_undp, "ICRC": src_icrc, "IOM": src_iom,
    "Boehringer Ingelheim": src_boehringer, "Merck KGaA": src_merck_kgaa,
    "Health companies": src_health_companies, "Himalayas": src_himalayas, "Remotive": src_remotive,
    "RemoteOK": src_remoteok, "Working Nomads": src_workingnomads, "Jobicy": src_jobicy,
}


# ---------------------------------------------------------------- scoring

def rx(patterns):
    return re.compile("|".join(patterns), re.I)


ROLE_TITLE = rx(PROFILE["role_keywords"])
ROLE_TEXT = rx(PROFILE["role_text"])
HOME = rx(PROFILE["home_country"] + PROFILE["home_area"])  # anywhere in Egypt (remote eligibility)
CAIRO = rx(PROFILE["home_area"])                             # where on-site and hybrid jobs must be
OTHER_CITY = rx(PROFILE["other_egypt_cities"])
COND_ROLE = rx(PROFILE["conditional_role_keywords"])
PHARMA_CTX = rx(PROFILE["pharma_context"])
HIDE_TITLE = rx(PROFILE["hide_titles"])
HIDE_ORG = rx(PROFILE["hide_orgs"])
HIDE_LOC = rx(PROFILE["hide_locations"])
HIDE_TEXT = rx(PROFILE["hide_text"])
ARABIC = rx(PROFILE["arabic_patterns"])
NATIVE_ENGLISH = rx(PROFILE["native_english_patterns"])
SENIOR = rx(PROFILE["senior_patterns"])
AREAS = {name: (rx(cfg["patterns"]), cfg["weight"]) for name, cfg in PROFILE["areas"].items()}
AREA_CAP = 30  # most a job can gain from practice areas

OTHER_LANGS = rx([rf"\b{l}\b" for l in PROFILE["other_languages"]])
NOT_SUITABLE = rx(PROFILE["not_suitable_titles"])

# Remote, but limited to people in another country
_PLACE = (r"(?:US|USA|U\.S\.(?:A\.)?|UK|EU|(?i:the united states|united states|the united kingdom|united kingdom"
          r"|canada|europe|australia|new zealand|germany|france|spain|italy|japan|south korea|korea|india|brazil|mexico))")
RESTRICTED = re.compile(
    rf"\b(?:US|USA|UK|EU|(?i:canada|europe|united states))[- ](?i:only|based|residents?|citizens?)\b"
    rf"|(?i:must|need to|required to|have to|should)\s+(?i:be\s+)?(?i:currently\s+)?"
    rf"(?i:live|reside|residing|living|located|based|a resident)[^.;]{{0,30}}\b{_PLACE}"
    rf"|(?i:authori[sz]ed|eligible|legally able|right)\s+to\s+work\s+in\s+(?i:the\s+)?{_PLACE}"
    rf"|(?i:residents?|citizens?)\s+of\s+(?i:the\s+)?{_PLACE}"
    rf"|(?i:anywhere in)\s+(?i:the\s+)?{_PLACE}"
    # US pharmacist licences: remote US pharmacy jobs need one
    r"|(?i:state (?:board of )?pharmacy licen[cs]e|licen[cs]ed (?:as a )?pharmacist in (?:the state|any (?:us |u\.s\. )?state|good standing in)"
    r"|\bnabp\b|\bnaplex\b|board of pharmacy)")
# "Remote, India" / "Germany - Remote": remote for one named country
HOME_BASED_IN = re.compile(r"home[- ]based\s*\(([^)]+)\)", re.I)
REMOTE_WORDS = re.compile(r"remote|home[- ]based|anywhere|worldwide|global|telecommut|work from home", re.I)
TIED_REMOTE = re.compile(r"^\s*remote\s*[,\-–|:]\s*([^()]+?)\s*$|^\s*([^()]+?)\s*[,\-–|]\s*remote\s*$", re.I)
OPEN_WORLD = rx([r"worldwide", r"anywhere in the world", r"from anywhere", r"any country", r"all countries",
                 r"\begypt\b", r"\bmena\b"])

# Key facts shown on the dashboard
QUALS = [("PharmD", r"\bpharm\.?\s?d\b|doctor of pharmacy"),
         ("B.Pharm", r"\bb\.?\s?pharm\b|bachelor'?s?(?: degree)? (?:of|in) pharma|pharmacy degree|degree in pharma"),
         ("Pharmacist licence", r"licen[cs]ed pharmacist|pharmacist licen[cs]e|registered pharmacist|licen[cs]e to practi[cs]e|syndicate"),
         ("GCP", r"\bgcp\b|good clinical practice")]
WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
         "twelve": 12, "fifteen": 15}
YEARS = re.compile(r"\b(\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten|twelve|fifteen)\b(?:\s*\(\d{1,2}\))?"
                   r"\s*\+?\s*(?:(?:-|–|to|or)\s*\d{1,2}\s*)?\+?\s*years?['’]?\s+(?:of\s+)?(?:[\w,/-]+\s+){0,5}?experience", re.I)
CONTRACTS = [("Internship", r"\bintern\b|\binternship"), ("Volunteer", r"\bvolunteer"),
             ("Freelance", r"freelance|independent contractor"), ("Consultancy", r"consultan|individual contractor"),
             ("Part-time", r"part[- ]?time"), ("Full-time", r"full[- ]?time"),
             ("Contract", r"contractor|fixed[- ]term|temporary|short[- ]term")]
_MONEY = r"(?:US\$|\$|€|£|EGP\s?|USD\s?|EUR\s?|GBP\s?)\d[\d,.]*k?"
PAY = re.compile(rf"{_MONEY}(?:\s*(?:-|–|to)\s*{_MONEY})?\s*(?:/|per\s+|an?\s+)\s*(?:hour|hr|month|year|annum|word|minute|min|day)\b", re.I)


EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
NOT_APPLY_EMAIL = re.compile(r"^(no-?reply|do-?not-?reply|privacy|dpo|gdpr|data|support|help|abuse|press|media|legal|accessibility|ethics|fraud)\b|@example\.", re.I)
APPLY_WORDS = re.compile(r"\bappl(y|ication|ications|icants?)\b|\bcv\b|\br[eé]sum[eé]\b|\bsubmit\b|\bsend\b|\bemail (us|your)\b|\bcandidat", re.I)


def apply_email(text):
    """An address the posting itself gives for applying, never one found anywhere else."""
    for m in EMAIL.finditer(text):
        address = m.group(0).rstrip(".")
        nearby = text[max(0, m.start() - 120):m.end() + 40]
        if not NOT_APPLY_EMAIL.search(address) and APPLY_WORDS.search(nearby):
            return address
    return ""


def key_facts(j):
    title, text = j["title"], j["text"]
    facts = {}
    quals = [name for name, pattern in QUALS if re.search(pattern, f"{title} {text}", re.I)]
    if quals:
        facts["quals"] = ", ".join(quals)
    m = YEARS.search(text)
    if m:
        n = WORDS.get(m.group(1).lower()) or int(m.group(1))
        if 1 <= n <= 25:
            facts["years"] = n
    head = f"{title} {j['level']}"
    for name, pattern in CONTRACTS:
        if re.search(pattern, head, re.I):
            facts["contract"] = name
            break
    else:
        for name, pattern in CONTRACTS:
            if re.search(pattern, text[:1200], re.I):
                facts["contract"] = name
                break
    m = PAY.search(f"{title} {text}")
    if m:
        facts["pay"] = re.sub(r"\s+", " ", m.group(0))[:32]
    return facts

OK_GEO = rx([r"worldwide", r"anywhere", r"global", r"egypt", r"africa", r"middle east", r"\bmena\b",
             r"\bemea\b", r"international", r"all countries", r"\bremote\b$"])
BLOCK_GEO = rx([r"\bus(a)?\b", r"united states", r"america", r"canada", r"\buk\b", r"united kingdom",
                r"europe", r"\beu\b", r"latam", r"latin", r"apac", r"asia", r"australia", r"india",
                r"germany", r"france", r"spain", r"poland", r"brazil", r"mexico", r"philippines",
                r"\bcet\b", r"\best\b", r"\bpst\b", r"north america"])


EXCLUDE_NAME = rx(PROFILE["exclude_title_or_company"]) if PROFILE.get("exclude_title_or_company") else None
EXCLUDE_TEXT = rx(PROFILE["exclude_description"]) if PROFILE.get("exclude_description") else None


def is_ai_gig(j):
    if EXCLUDE_NAME and EXCLUDE_NAME.search(f"{j['title']} {j['org']}"):
        return True
    # UN and NGO postings can discuss AI as a topic, so only check commercial boards.
    return bool(EXCLUDE_TEXT and not j["intl"] and EXCLUDE_TEXT.search(j["text"]))


def hidden(j):
    """Jobs that never fit: retail pharmacy, sales and field roles, technician and assistant
    roles, travel-based roles and jobs needing a licence from another country."""
    return bool(HIDE_TITLE.search(j["title"]) or HIDE_ORG.search(j["org"]) or HIDE_LOC.search(j["location"] or "")
                or HIDE_TEXT.search(f"{j['title']} {j['text']}"))


def score(j):
    if is_ai_gig(j) or NOT_SUITABLE.search(j["title"]) or hidden(j):
        return None
    title, text = j["title"], j["text"]
    full = f"{title} {text}"
    s, reasons, areas = 0, [], []

    loc = j["location"] or ""
    in_cairo = bool(CAIRO.search(loc))
    in_egypt = bool(HOME.search(loc))
    other_city = in_egypt and not in_cairo and bool(OTHER_CITY.search(loc))
    if j["allowed"] and not any(HOME.search(a) for a in j["allowed"]):
        return None  # remote, but only for residents of listed countries
    if not j["remote"]:
        if not in_egypt or other_city:
            return None  # on-site and hybrid jobs must be in Greater Cairo
    elif not in_egypt:
        tied = TIED_REMOTE.match(loc) or HOME_BASED_IN.search(loc)
        place = tied and next(g for g in tied.groups() if g)
        if place and not OK_GEO.search(place):
            return None  # remote for one named place, e.g. "Remote, India" or "Home-based (Brazil)"
        if loc and not OK_GEO.search(loc) and BLOCK_GEO.search(loc):
            return None  # remote, but only for people in e.g. the US or Europe
        if RESTRICTED.search(full):
            return None  # the posting itself says e.g. "US residents only"
        if j["check_location"] and loc and not (OK_GEO.search(loc) or REMOTE_WORDS.search(loc)):
            return None  # job boards that tag a city or country (e.g. "Lisbon, Portugal") mean that place
        if (not loc or not OK_GEO.search(loc)) and not OPEN_WORLD.search(full):
            reasons.append("Check country eligibility")
    if j["deadline"] and j["deadline"] < NOW - timedelta(hours=12):
        return None
    if j["posted"] and not j["deadline"] and j["posted"] < NOW - timedelta(days=PROFILE["max_age_days"]):
        return None

    # Quality and supply titles only count when the posting is about medicines,
    # whoever the employer is ("inventory" at a climate agency is not pharmacy work).
    role = bool(ROLE_TITLE.search(title)) or bool(COND_ROLE.search(title) and PHARMA_CTX.search(full))
    pharm_text = bool(ROLE_TEXT.search(text))
    if role:
        s += 40
        reasons.insert(0, "Pharmacy role")
    elif pharm_text:
        s += 15
        reasons.insert(0, "Asks for a pharmacy background")
    else:
        return None  # not a pharmacy job

    gain = 0
    for name, (pat, w) in AREAS.items():
        if pat.search(title):
            gain += w + 8
            areas.append(name)
        elif pat.search(text):
            gain += w
            areas.append(name)
    s += min(gain, AREA_CAP)
    if "Hospital & clinical" in areas:
        s += 10
        reasons.append("Matches your hospital experience")

    if ARABIC.search(full):
        s += 10
        reasons.append("Arabic is a plus")
    if NATIVE_ENGLISH.search(full):
        s -= 15
        reasons.append("Needs native-level English")
    other = OTHER_LANGS.search(title)  # e.g. "German-speaking Drug Safety Associate"
    if other:
        s -= 30
        reasons.append(f"Needs {other.group(0).title()}")

    if not j["remote"] and in_cairo:
        s += 20
        reasons.append("Based in Greater Cairo")
    elif not j["remote"]:
        s += 10
        reasons.append("Check it's in Greater Cairo")
    elif in_egypt:
        s += 10
        reasons.append("Open to Egypt")
    if j["remote"]:
        s += 15
        reasons.append("Remote / home-based")
    if j["intl"]:
        s += 10
    elif j["pharma"]:
        s += 5
    if SENIOR.search(title) or SENIOR.search(j["level"]):
        s -= 15
        reasons.append("Senior level")
    years = key_facts(j).get("years", 0)
    if years >= 7:
        s -= 20
        reasons.append("Asks 7+ years")
    elif years >= 5:
        s -= 12
        reasons.append("Asks 5+ years")
    if re.search(r"\bintern\b|\binternship", title, re.I):
        reasons.append("Internships are usually for students")

    if s < PROFILE["min_score"]:
        return None
    return max(0, min(100, s)), reasons, areas


# ---------------------------------------------------------------- pipeline

def run_source(fn):
    """One retry, because big pages sometimes time out or come back empty."""
    try:
        got = fn()
        if got:
            return got
    except FeedSkipped:
        raise
    except Exception:
        pass
    time.sleep(5)
    return fn()


def key(j):
    # The same UN job can appear under different org spellings on different sites.
    title = re.sub(r"[^a-z0-9]", "", j["title"].lower())
    return title[:80] if j["intl"] and len(title) >= 25 else (title + re.sub(r"[^a-z0-9]", "", j["org"].lower())[:20])[:80]


def same_post(j):
    """UN jobs often appear on two sites with different org spellings and a grade
    suffix, e.g. "Procurement Officer" and "Procurement Officer, P3"."""
    title = re.sub(r"[,(]?\s*\b(?:[pgd]|no|gs)-?\d\b\)?|\(.*?\)", "", j["title"].lower())
    place = re.split(r"[,|/-]", (j["location"] or "").lower())[0].strip()
    return re.sub(r"[^a-z0-9]", "", title), place


def merge_duplicates(ordered):
    """Keep the best-scoring copy of each UN post (the list is already best first).
    Copies must share title and place. Their closing dates must be within 2 days of each
    other when both have one, because sites store deadlines in different time zones."""
    kept, out = {}, []
    for j in ordered:
        if j["intl"]:
            day = parse_date(j["deadline"])
            days = kept.setdefault(same_post(j), [])
            if any(not d or not day or abs(d - day) <= timedelta(days=2) for d in days):
                continue
            days.append(day)
        out.append(j)
    return out


def main():
    DEADLINE[0] = time.time() + RUN_LIMIT
    previous, seen, quote_index, old_links, old_health = {}, {}, 0, {}, {}
    if OUT.exists():
        try:
            old_data = json.loads(OUT.read_text(encoding="utf-8"))
            previous = {p["id"]: p for p in old_data["jobs"]}
            seen = old_data.get("seen", {})
            quote_index = old_data.get("quote_index", 0)
            old_links = old_data.get("links", {})
            old_health = old_data.get("health", {})
        except Exception:
            pass
    # Every job ever listed -> when it was first seen, so a job that vanishes for a
    # while (site outage, temporary delisting) is not announced as new when it returns.
    seen.update({i: p["first_seen"] for i, p in previous.items() if i not in seen})

    raw, status = [], {}
    with cf.ThreadPoolExecutor(max_workers=len(SOURCES)) as ex:
        futs = {ex.submit(run_source, fn): name for name, fn in SOURCES.items()}
        for f in cf.as_completed(futs):
            name = futs[f]
            try:
                got = f.result()
                for j in got:
                    j["feed"] = name
                raw += got
                status[name] = {"ok": bool(got) or name in PREFILTERED, "count": len(got)}
                if not status[name]["ok"]:
                    status[name]["error"] = "returned no jobs (site may have changed)"
            except FeedSkipped as e:
                status[name] = {"ok": e.ok, "count": 0, "kept": True, "error": str(e)[:200]}
            except Exception as e:
                status[name] = {"ok": False, "error": str(e)[:200]}
            print(f"{name:16} {status[name]}", file=sys.stderr)

    jobs = {}
    for j in raw:
        if not j["url"] or not j["title"]:
            continue
        r = score(j)
        if not r:
            continue
        s, reasons, areas = r
        k = key(j)
        if k in jobs and jobs[k]["score"] >= s:
            continue
        jobs[k] = {
            "id": k, "title": j["title"], "org": j["org"] or j["source"], "source": j["source"],
            "feed": j["feed"],
            "url": j["url"], "location": j["location"], "remote": j["remote"],
            "egypt": any(r in reasons for r in ("Based in Greater Cairo", "Check it's in Greater Cairo", "Open to Egypt")),
            "intl": j["intl"], "pharma": j["pharma"],
            "deadline": iso(j["deadline"]), "posted": iso(j["posted"]),
            "score": s, "reasons": reasons, "areas": areas,
            "role": "Pharmacy role" in reasons,
            "facts": key_facts(j),
            "apply_email": apply_email(j["text"]),
            "snippet": j["text"][:420],
            "details": j["text"][:1800],
            "first_seen": seen.get(k, iso(NOW)),
        }

    # A source that failed today keeps yesterday's jobs instead of emptying the list.
    failed = {name for name, st in status.items() if not st["ok"] or st.get("kept")}
    for k, p in previous.items():
        if k not in jobs and p.get("feed", p["source"]) in failed and (p["deadline"] or "9") >= iso(NOW):
            jobs[k] = p

    ordered = merge_duplicates(sorted(jobs.values(), key=lambda x: (-x["score"], x["deadline"] or "9")))
    OUT.parent.mkdir(exist_ok=True)
    new = [j for j in ordered if j["id"] not in seen]
    seen.update({j["id"]: j["first_seen"] for j in ordered})
    cutoff = iso(NOW - timedelta(days=SEEN_DAYS))
    seen = {i: t for i, t in seen.items() if t >= cutoff or i in jobs}
    alerts = [j for j in new if j["score"] >= PROFILE["notify_min_score"]]
    quote = next_quote(quote_index) if alerts else ""
    links, broken_now = check_direct_links(old_links)
    health, problems, recovered = source_health(status, old_health)
    admin = "\n\n".join(m for m in (sources_message(problems, recovered, len(ordered)),
                                     broken_links_message(broken_now)) if m)
    OUT.write_text(json.dumps({"updated": iso(NOW), "areas": list(AREAS), "sources": status, "jobs": ordered, "seen": seen,
                               "quote_index": quote_index + bool(alerts), "links": links,
                               "health": health},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n{len(raw)} fetched -> {len(ordered)} matches ({len(new)} new) -> {OUT}", file=sys.stderr)
    if os.environ.get("DEFER_NOTIFY"):
        # The workflow sends these after the website has the new list (see send_outbox).
        if alerts or admin:
            OUTBOX.write_text(json.dumps({"alerts": alerts, "total": len(ordered), "quote": quote,
                                          "updated": iso(NOW), "admin": admin}, ensure_ascii=False), encoding="utf-8")
    else:
        notify(alerts, len(ordered), quote, iso(NOW))
        admin_message(admin)


# ---------------------------------------------------------------- problem alerts

SOURCE_FAIL_DAYS = 2  # report a source after this many failed runs in a row


def source_health(status, old):
    """Count failed runs in a row per source; report new failures once and recoveries once."""
    health, problems, recovered = {}, [], []
    for name, st in status.items():
        before = old.get(name, 0)
        health[name] = before + 1 if not st["ok"] else 0
        if health[name] == SOURCE_FAIL_DAYS:
            problems.append((name, st.get("error", "")))
        if st["ok"] and before >= SOURCE_FAIL_DAYS:
            recovered.append(name)
    return health, problems, recovered


def sources_message(problems, recovered, matches):
    e = html.escape
    parts = []
    if problems:
        parts.append(f"<b>⚠️ {len(problems)} job source{'s' if len(problems) > 1 else ''} failed "
                     f"{SOURCE_FAIL_DAYS} runs in a row</b>\n" +
                     "\n".join(f"• {e(n)}: {e(err[:120]) or 'no details'}" for n, err in problems) +
                     "\nOther sources still run, and the last jobs from these stay on the list.")
    if recovered:
        parts.append("✅ Working again: " + ", ".join(e(n) for n in recovered))
    if matches == 0:
        parts.append("<b>⚠️ Today's run found no matching jobs at all.</b> Something may be wrong with the filters or sources.")
    return "\n\n".join(parts)


# ---------------------------------------------------------------- Apply directly link checks

LINK_FAIL_DAYS = 2  # alert after this many failed checks in a row, so a one-day outage stays quiet


def probe(url):
    """'ok', 'broken' (missing page or unreachable site) or 'unknown' (the site blocks automated checks)."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,*/*"})
        with urllib.request.urlopen(req, timeout=25) as r:
            return "ok", r.status
    except urllib.error.HTTPError as e:
        return ("broken" if e.code in (404, 410) else "unknown"), e.code
    except Exception as e:
        return "broken", type(e).__name__


def check_direct_links(old):
    path = ROOT / "docs" / "direct.json"
    if not path.exists():
        return {}, []
    places = [p for g in json.loads(path.read_text(encoding="utf-8")) for p in g["items"]]
    with cf.ThreadPoolExecutor(max_workers=6) as ex:
        results = list(ex.map(lambda p: probe(p["link"]), places))
    links, broken_now = {}, []
    for p, (state, code) in zip(places, results):
        fails = (old.get(p["id"], {}).get("fails", 0) + 1) if state == "broken" else 0
        links[p["id"]] = {"state": state, "code": code, "fails": fails}
        if fails == LINK_FAIL_DAYS:
            broken_now.append((p, code))
    print(f"Apply directly links: {sum(v['state'] == 'ok' for v in links.values())} ok, "
          f"{sum(v['state'] == 'unknown' for v in links.values())} can't be checked, "
          f"{sum(v['state'] == 'broken' for v in links.values())} failing", file=sys.stderr)
    return links, broken_now


def broken_links_message(broken):
    if not broken:
        return ""
    e = html.escape
    lines = [f"<b>🔧 {len(broken)} Apply directly link{'s' if len(broken) > 1 else ''} stopped working</b>",
             f"They failed {LINK_FAIL_DAYS} days in a row and may have moved:\n"]
    lines += [f"• <a href=\"{e(p['link'])}\">{e(p['name'])}</a> ({e(str(code))})" for p, code in broken]
    lines.append("\nUpdate them in docs/direct.json, or ask for help finding the new pages.")
    return "\n".join(lines)


def admin_message(text):
    """Maintenance messages go to ADMIN_CHAT_ID, or the first chat in TELEGRAM_CHAT_ID."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chats = [c.strip() for c in os.environ.get("TELEGRAM_CHAT_ID", "").split(",") if c.strip()]
    chat = os.environ.get("ADMIN_CHAT_ID") or (chats[0] if chats else "")
    if not (text and token and chat):
        return
    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=urllib.parse.urlencode({"chat_id": chat, "text": text, "parse_mode": "HTML",
                                         "disable_web_page_preview": "true"}).encode())
        urllib.request.urlopen(req, timeout=20).read()
        print("Maintenance message sent", file=sys.stderr)
    except Exception as ex:
        print(f"Maintenance message failed: {type(ex).__name__}", file=sys.stderr)


def send_outbox():
    """Send the saved alerts once the dashboard serves this run's list, so job links open
    straight away. Gives up waiting after 15 minutes and sends anyway."""
    if not OUTBOX.exists():
        print("No alerts to send", file=sys.stderr)
        return
    box = json.loads(OUTBOX.read_text(encoding="utf-8"))
    dash = os.environ.get("DASHBOARD_URL", "")
    deadline = time.time() + 15 * 60
    while dash and time.time() < deadline:
        try:
            live = http_json(f"{dash}jobs.json?fresh={int(time.time())}", retries=0, timeout=20).get("updated")
            if live and live >= box["updated"]:
                print("Dashboard is up to date", file=sys.stderr)
                break
        except Exception:
            pass
        time.sleep(20)
    else:
        if dash:
            print("Dashboard still updating, sending anyway", file=sys.stderr)
    notify(box["alerts"], box["total"], box["quote"], box["updated"])
    admin_message(box.get("admin", ""))


# ---------------------------------------------------------------- notifications (free)

def next_quote(index):
    """The next intro from quotes.txt. The position is saved, so none repeats until all have been used."""
    path = ROOT / "quotes.txt"
    quotes = [q.strip() for q in path.read_text(encoding="utf-8").splitlines() if q.strip()] if path.exists() else []
    return quotes[index % len(quotes)] if quotes else ""


def notify(new, total, quote="", updated=""):
    if not new:
        return
    dash = os.environ.get("DASHBOARD_URL", "")
    top = new[:12]

    def link(j):
        """Open the job's details in the dashboard, or the posting if there is no dashboard."""
        # u= lets the dashboard wait for this list if a link is opened before the site updates
        return f"{dash}#job={urllib.parse.quote(j['id'])}&u={updated}" if dash else j["url"]

    token, chats = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID", "")
    if token and chats:
        e = html.escape
        lines = [f"<i>{e(quote)}</i>\n"] if quote else []
        lines.append(f"<b>💊 {len(new)} new job match{'es' if len(new) > 1 else ''}</b>\n")
        for j in top:
            dl = f" · ⏳ {j['deadline'][:10]}" if j["deadline"] else ""
            where = "🏠 Remote" if j["remote"] else "📍 " + e(j["location"][:40])
            lines.append(f"<b>{j['score']}</b> · <a href=\"{e(link(j))}\">{e(j['title'][:110])}</a>\n"
                         f"   {e(j['org'][:60])} · {where}{dl}")
        if dash:
            lines.append(f"\n📱 <a href=\"{e(dash)}\">Open dashboard</a> ({total} open matches)")
        for chat in [c.strip() for c in chats.split(",") if c.strip()]:
            try:
                req = urllib.request.Request(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    data=urllib.parse.urlencode({"chat_id": chat, "text": "\n".join(lines),
                                                 "parse_mode": "HTML",
                                                 "disable_web_page_preview": "true"}).encode())
                urllib.request.urlopen(req, timeout=20).read()
                print("Telegram message sent", file=sys.stderr)
            except Exception as ex:
                print(f"Telegram failed: {type(ex).__name__}", file=sys.stderr)

    user, pw, to = os.environ.get("SMTP_USER"), os.environ.get("SMTP_PASSWORD"), os.environ.get("EMAIL_TO")
    if user and pw and to:
        rows = "".join(
            f"<p><b>{j['score']}</b> &middot; <a href=\"{html.escape(link(j))}\">{html.escape(j['title'])}</a><br>"
            f"<small>{html.escape(j['org'])} &middot; {'Remote' if j['remote'] else html.escape(j['location'])}"
            f"{' &middot; closes ' + j['deadline'][:10] if j['deadline'] else ''}</small></p>" for j in top)
        if quote:
            rows = f"<p><i>{html.escape(quote)}</i></p>" + rows
        if dash:
            rows += f'<p><a href="{html.escape(dash)}">Open the dashboard</a></p>'
        msg = MIMEText(rows, "html", "utf-8")
        msg["Subject"] = f"Pharmacy Job Tracker: {len(new)} new matches"
        msg["From"], msg["To"] = user, to
        try:
            with smtplib.SMTP_SSL(os.environ.get("SMTP_HOST", "smtp.gmail.com"), 465, timeout=30) as smtp:
                smtp.login(user, pw)
                smtp.sendmail(user, [t.strip() for t in to.split(",")], msg.as_string())
            print("Email sent", file=sys.stderr)
        except Exception as ex:
            print(f"Email failed: {ex}", file=sys.stderr)


if __name__ == "__main__":
    send_outbox() if sys.argv[1:] == ["send"] else main()
