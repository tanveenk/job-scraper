#!/usr/bin/env python3
"""
Internship alerts: polls company ATS boards, the SimplifyJobs repo, and LinkedIn's
public (logged-out) job search, then emails you a digest of anything new.

Setup:
    pip install -r requirements.txt
    copy .env.example to .env and fill it in (the script loads .env automatically)

Run once:      python internship_alerts.py
Run forever:   python internship_alerts.py --loop
Cloud:         see .github/workflows/alerts.yml (secrets come from GitHub, not .env)
The first run only indexes what already exists (no flood of emails) and sends one test email.
"""
import argparse
import logging
import os
import random
import re
import smtplib
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from email.message import EmailMessage
from html import escape

import requests
from bs4 import BeautifulSoup


def load_env(path=".env"):
    """Tiny .env reader so you don't need to export variables each time."""
    if not os.path.exists(path):
        return
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


# ----------------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------------
ATS_INTERVAL_SEC = 300        # company boards + Simplify: every 5 minutes
LINKEDIN_INTERVAL_SEC = 1500  # LinkedIn: roughly every 25-30 minutes (keep this slow)
DB_PATH = "seen_jobs.db"

# Title must look like an internship AND like one of your roles.
LEVEL_RE = re.compile(r"\b(intern|interns|internship|co-?op|coop|student)\b", re.I)
ROLE_RE = re.compile(
    r"(software|swe|developer|engineer|full[- ]?stack|front[- ]?end|back[- ]?end|"
    r"data|analyst|analytics|scientist|machine learning|\bml\b|\bai\b|"
    r"product|project|program manager|business intelligence|\bbi\b|technology|\bit\b|cyber|security)",
    re.I,
)
EXCLUDE_RE = re.compile(r"\b(senior|sr\.?|staff|principal|director|vp|head of|lead)\b", re.I)

# Set to None to accept every location. Default: Canada + remote.
LOCATION_RE = re.compile(
    r"(canada|\bbc\b|british columbia|vancouver|burnaby|surrey|victoria|toronto|ottawa|"
    r"waterloo|montreal|calgary|edmonton|\bon\b|\bqc\b|\bab\b|remote)",
    re.I,
)

# Simplify repo listings.json. Check the repo for the current season's branch/path.
SIMPLIFY_URLS = [
    "https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/.github/scripts/listings.json",
]

# Company board slugs: the bit in the careers URL.
#   boards.greenhouse.io/<slug>   jobs.lever.co/<slug>   jobs.ashbyhq.com/<slug>
# Wrong or missing slugs are skipped quietly. Add as many as you like.
GREENHOUSE = [
    "stripe", "airbnb", "databricks", "figma", "coinbase", "cloudflare", "discord", "duolingo",
    "dropbox", "reddit", "robinhood", "instacart", "asana", "lyft", "pinterest", "twilio", "okta",
    "mongodb", "elastic", "datadog", "gitlab", "samsara", "anthropic", "scaleai", "brex", "affirm",
    "airtable", "hubspotjobs", "toast", "squarespace", "peloton", "etsy", "gusto", "flexport",
    "doordash", "uber", "wealthsimple", "faire", "carta", "chime", "mixpanel", "amplitude",
]
LEVER = ["palantir", "spotify", "plaid", "kraken", "weave", "wealthsimple", "clearco", "ritual"]
ASHBY = ["openai", "notion", "ramp", "linear", "supabase", "cohere", "1password", "deel", "retool",
         "vanta", "watershed", "replit", "perplexity", "mercor"]

# Workday: (company, host, site). Find yours: open a careers page, the URL looks like
# https://<tenant>.wd<N>.myworkdayjobs.com/<site>
WORKDAY = [
    ("RBC", "rbc.wd3.myworkdayjobs.com", "RBCGlobal1"),
    ("Workday", "workday.wd5.myworkdayjobs.com", "Workday"),
]

# LinkedIn logged-out search queries.
LINKEDIN_QUERIES = [
    "software engineer intern", "software developer co-op", "full stack intern",
    "product manager intern", "project manager intern", "data analyst intern",
    "data scientist intern", "data engineer intern", "business analyst co-op",
    "machine learning intern",
]
LINKEDIN_LOCATION = "Canada"
LINKEDIN_WINDOW_SEC = 7200  # only jobs posted in the last 2 hours (overlaps the 30 min poll)

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36"}
log = logging.getLogger("alerts")


# ----------------------------------------------------------------------------
# FETCHERS: each returns a list of dicts: id, source, company, title, location, url
# ----------------------------------------------------------------------------
def job(source, jid, company, title, location, url):
    return {"id": f"{source}:{jid}", "source": source, "company": company or "",
            "title": (title or "").strip(), "location": (location or "").strip(), "url": url or ""}


def fetch_simplify(url):
    out = []
    try:
        r = requests.get(url, headers=UA, timeout=30)
        r.raise_for_status()
        for j in r.json():
            if not j.get("active", True) or j.get("is_visible") is False:
                continue
            out.append(job("simplify", j.get("id") or j.get("url"), j.get("company_name"),
                           j.get("title"), ", ".join(j.get("locations") or []), j.get("url")))
    except Exception as e:
        log.warning("simplify failed (%s): %s", url, e)
    return out


def fetch_greenhouse(slug):
    try:
        r = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", headers=UA, timeout=20)
        if r.status_code != 200:
            return []
        return [job("greenhouse", f"{slug}-{j['id']}", slug.title(), j["title"],
                    (j.get("location") or {}).get("name"), j.get("absolute_url"))
                for j in r.json().get("jobs", [])]
    except Exception as e:
        log.debug("greenhouse %s: %s", slug, e)
        return []


def fetch_lever(slug):
    try:
        r = requests.get(f"https://api.lever.co/v0/postings/{slug}?mode=json", headers=UA, timeout=20)
        if r.status_code != 200:
            return []
        return [job("lever", j["id"], slug.title(), j["text"],
                    (j.get("categories") or {}).get("location"), j.get("hostedUrl"))
                for j in r.json()]
    except Exception as e:
        log.debug("lever %s: %s", slug, e)
        return []


def fetch_ashby(slug):
    try:
        r = requests.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}", headers=UA, timeout=20)
        if r.status_code != 200:
            return []
        return [job("ashby", j["id"], slug.title(), j["title"], j.get("location"), j.get("jobUrl"))
                for j in r.json().get("jobs", [])]
    except Exception as e:
        log.debug("ashby %s: %s", slug, e)
        return []


def fetch_workday(entry):
    company, host, site = entry
    tenant = host.split(".")[0]
    api = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    out = []
    for offset in (0, 20, 40):
        try:
            r = requests.post(api, headers={**UA, "Content-Type": "application/json"}, timeout=20,
                              json={"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": "intern"})
            if r.status_code != 200:
                break
            posts = r.json().get("jobPostings", [])
            for p in posts:
                path = p.get("externalPath", "")
                out.append(job("workday", f"{tenant}-{path}", company, p.get("title"),
                               p.get("locationsText"), f"https://{host}/en-US/{site}{path}"))
            if len(posts) < 20:
                break
        except Exception as e:
            log.debug("workday %s: %s", company, e)
            break
    return out


def fetch_linkedin():
    """LinkedIn's public guest endpoint. Never logs in: no credentials, no cookies, no session.
    Each request is a plain anonymous GET, so none of your LinkedIn accounts is ever involved.
    Slow and polite on purpose; stops for the round on any block."""
    out = []
    for q in LINKEDIN_QUERIES:
        try:
            r = requests.get(
                "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search",
                params={"keywords": q, "location": LINKEDIN_LOCATION,
                        "f_TPR": f"r{LINKEDIN_WINDOW_SEC}", "start": 0},
                headers=UA, timeout=20)
            if r.status_code in (429, 999, 403):
                log.warning("LinkedIn blocked/rate-limited (%s). Skipping the rest of this round.", r.status_code)
                break
            if r.status_code != 200:
                continue
            soup = BeautifulSoup(r.text, "html.parser")
            for card in soup.select("div.base-card, li"):
                a = card.select_one("a.base-card__full-link")
                t = card.select_one(".base-search-card__title")
                if not (a and t):
                    continue
                c = card.select_one(".base-search-card__subtitle")
                loc = card.select_one(".job-search-card__location")
                url = a["href"].split("?")[0]
                jid = re.search(r"(\d{6,})", url)
                out.append(job("linkedin", jid.group(1) if jid else url,
                               c.get_text(strip=True) if c else "", t.get_text(strip=True),
                               loc.get_text(strip=True) if loc else "", url))
        except Exception as e:
            log.warning("linkedin query '%s' failed: %s", q, e)
        time.sleep(random.uniform(6, 12))
    return out


# ----------------------------------------------------------------------------
# FILTER + STORAGE + EMAIL
# ----------------------------------------------------------------------------
def wanted(j):
    t = j["title"]
    if not (LEVEL_RE.search(t) and ROLE_RE.search(t)) or EXCLUDE_RE.search(t):
        return False
    if LOCATION_RE and j["location"] and not LOCATION_RE.search(j["location"]):
        return False
    return True


def fingerprint(j):
    return "fp:" + re.sub(r"\W+", "", f"{j['company']}{j['title']}".lower())


def open_db():
    db = sqlite3.connect(DB_PATH)
    db.execute("CREATE TABLE IF NOT EXISTS seen (key TEXT PRIMARY KEY, ts REAL)")
    db.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
    return db


def filter_new(db, jobs):
    new, batch_keys = [], set()
    for j in jobs:
        if not wanted(j):
            continue
        keys = (j["id"], fingerprint(j))  # fingerprint catches the same job on 2 sources
        if any(k in batch_keys or db.execute("SELECT 1 FROM seen WHERE key=?", (k,)).fetchone() for k in keys):
            continue
        batch_keys.update(keys)
        new.append(j)
    return new


def mark_seen(db, jobs):
    now = time.time()
    for j in jobs:
        for k in (j["id"], fingerprint(j)):
            db.execute("INSERT OR IGNORE INTO seen VALUES (?, ?)", (k, now))
    db.commit()


def send_email(subject, jobs=None, note=None):
    user, pw, to = os.environ.get("SMTP_USER"), (os.environ.get("SMTP_PASS") or "").replace(" ", ""), os.environ.get("EMAIL_TO")
    if not (user and pw and to):
        log.error("Set SMTP_USER, SMTP_PASS and EMAIL_TO to send email.")
        return
    jobs = jobs or []
    text = (note + "\n\n" if note else "") + "\n\n".join(
        f"{j['company']} | {j['title']}\n{j['location']} ({j['source']})\n{j['url']}" for j in jobs)
    rows = "".join(
        f"<p><b>{escape(j['title'])}</b><br>{escape(j['company'])} &middot; {escape(j['location'])} "
        f"&middot; <i>{j['source']}</i><br><a href='{escape(j['url'])}'>Apply</a></p>" for j in jobs)
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, user, to
    msg.set_content(text)
    msg.add_alternative(f"<html><body>{'<p>'+escape(note)+'</p>' if note else ''}{rows}</body></html>", subtype="html")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(user, pw)
        s.send_message(msg)


def poll_ats():
    jobs = []
    with ThreadPoolExecutor(max_workers=16) as ex:
        futs = [ex.submit(fetch_simplify, u) for u in SIMPLIFY_URLS]
        futs += [ex.submit(fetch_greenhouse, s) for s in GREENHOUSE]
        futs += [ex.submit(fetch_lever, s) for s in LEVER]
        futs += [ex.submit(fetch_ashby, s) for s in ASHBY]
        futs += [ex.submit(fetch_workday, w) for w in WORKDAY]
        for f in futs:
            jobs += f.result()
    return jobs


def run_round(db, include_linkedin):
    jobs = poll_ats()
    if include_linkedin:
        jobs += fetch_linkedin()
    first_run = db.execute("SELECT COUNT(*) FROM seen").fetchone()[0] == 0
    new = filter_new(db, jobs)
    log.info("fetched %d, %d new matches", len(jobs), len(new))
    if first_run:
        mark_seen(db, new)
        send_email("Internship alerts: set up and running",
                   note=f"Indexed {len(new)} current matching listings. You'll only get emails for new ones from now on.")
        return
    if new:
        send_email(f"{len(new)} new internship posting{'s' if len(new) != 1 else ''}", new)
        mark_seen(db, new)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", action="store_true", help="keep running")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    load_env()
    db = open_db()
    while True:
        row = db.execute("SELECT v FROM meta WHERE k='last_li'").fetchone()
        last_li = float(row[0]) if row else 0.0
        do_li = os.environ.get("SKIP_LINKEDIN") != "1" and time.time() - last_li >= LINKEDIN_INTERVAL_SEC
        try:
            run_round(db, do_li)
        except Exception:
            log.exception("round failed")
        if do_li:
            db.execute("INSERT OR REPLACE INTO meta VALUES ('last_li', ?)", (str(time.time()),))
            db.commit()
        if not args.loop:
            break
        time.sleep(ATS_INTERVAL_SEC)


if __name__ == "__main__":
    main()