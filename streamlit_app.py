
import streamlit as st
import pandas as pd
import requests, random, re, sqlite3, time
from bs4 import BeautifulSoup
from urllib.parse import urlparse, quote
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail

st.set_page_config(page_title="Miami Flooring Prospector", layout="wide")
st.title("Miami Flooring Prospector — Streamlit Cloud Edition")

SENDER_NAME = st.secrets.get("SENDER_NAME", "Miami Master Flooring")
SENDER_EMAIL = st.secrets.get("SENDER_EMAIL", "info@miamimasterflooring.com")
REPLY_TO = st.secrets.get("REPLY_TO", "info@miamimasterflooring.com")
SENDGRID_API_KEY = st.secrets.get("SENDGRID_API_KEY")

EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
DB_PATH = "mfp.sqlite"

def db_conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False)

def init_db():
    with db_conn() as con:
        cur = con.cursor()
        cur.execute("""CREATE TABLE IF NOT EXISTS companies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT, email TEXT UNIQUE, website TEXT, phone TEXT, address TEXT,
            source TEXT, domain TEXT, score REAL DEFAULT 0
        )""")
        cur.execute("""CREATE TABLE IF NOT EXISTS suppression (
            email TEXT PRIMARY KEY
        )""")
        con.commit()
init_db()

def score_lead(rec):
    score = 0.0
    if rec.get("email"): score += 2.0
    if rec.get("phone"): score += 1.0
    if rec.get("address"): score += 1.0
    addr = (rec.get("address") or "").lower()
    for k, pts in [("miami",1.0), ("broward",0.5), ("palm beach",0.5)]:
        if k in addr: score += pts
    return score

HEADERS = [
    {"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115 Safari/537.36"},
    {"User-Agent":"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.5 Safari/605.1.15"}
]
GEO = "Miami OR 'Miami-Dade' OR 'South Florida' OR 'Broward County' OR 'Palm Beach County' OR Fort Lauderdale OR Hollywood OR Doral OR Hialeah OR 'Miami Beach' OR Homestead OR 'Coral Gables' OR Weston OR Miramar OR 'Pembroke Pines' OR Boca Raton"

def http_get(url):
    h = random.choice(HEADERS)
    r = requests.get(url, headers=h, timeout=15)
    r.raise_for_status()
    return r.text

def domain(url):
    try:
        return urlparse(url).netloc.lower()
    except Exception:
        return ""

def is_competitor(dom):
    return any(bad in dom for bad in ("floor","tile","carpet"))

def search_google(q):
    term = quote(f"{q} site:.com OR site:.net OR site:.org ({GEO})")
    url = f"https://www.google.com/search?q={term}&num=20&hl=en"
    html = http_get(url)
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for a in soup.select("a"):
        href = a.get("href","")
        if href.startswith("http") and "google" not in href:
            out.append(href)
    return out[:40]

def search_bing(q):
    term = quote(f"{q} site:.com OR site:.net OR site:.org ({GEO})")
    url = f"https://www.bing.com/search?q={term}&count=30"
    html = http_get(url)
    soup = BeautifulSoup(html, "html.parser")
    return [a["href"] for a in soup.select("li.b_algo h2 a") if a.get("href")][:40]

def search_ddg(q):
    term = quote(f"{q} site:.com OR site:.net OR site:.org ({GEO})")
    url = f"https://duckduckgo.com/html/?q={term}&kl=us-en"
    html = http_get(url)
    soup = BeautifulSoup(html, "html.parser")
    return [a.get("href") for a in soup.select("a.result__a") if a.get("href","").startswith("http")][:40]

def extract_company_info(u):
    data = {"website": u, "email": None, "phone": None, "address": None, "name": None}
    try:
        html = http_get(u)
    except Exception:
        return data
    soup = BeautifulSoup(html, "html.parser")
    if soup.title and soup.title.string:
        data["name"] = soup.title.string.split(" | ")[0].split(" – ")[0][:255]
    footers = soup.select("footer, .site-footer, .footer")
    text = " ".join([f.get_text(" ", strip=True) for f in footers]) or soup.get_text(" ", strip=True)
    emails = EMAIL_RE.findall(text)
    if emails: data["email"] = emails[0]
    phones = re.findall(r"\+?1?[\s\-\.\(]?\d{3}[\)\s\-\.\)]?\s?\d{3}\s?[\-\.\s]?\d{4}", text)
    if phones: data["phone"] = phones[0]
    addr = re.search(r"\d{2,5}\s+\w+.*(Miami|Broward|Palm Beach|Florida|FL)\b.*", text, re.IGNORECASE)
    if addr: data["address"] = addr.group(0)[:500]
    if not data["email"]:
        emails2 = EMAIL_RE.findall(soup.get_text(" ", strip=True))
        if emails2: data["email"] = emails2[0]
    return data

def upsert_company(rec):
    with db_conn() as con:
        cur = con.cursor()
        try:
            cur.execute("""INSERT OR IGNORE INTO companies (name,email,website,phone,address,source,domain,score)
                           VALUES (?,?,?,?,?,?,?,?)""",
                        (rec.get("name"), rec.get("email"), rec.get("website"),
                         rec.get("phone"), rec.get("address"), rec.get("source","scrape"),
                         rec.get("domain"), rec.get("score",0.0)))
            con.commit()
        except Exception:
            pass

def send_email_via_sendgrid(to_email, subject, html):
    if not SENDGRID_API_KEY:
        raise RuntimeError("SENDGRID_API_KEY not configured in Streamlit secrets")
    message = Mail(
        from_email=(SENDER_EMAIL, SENDER_NAME),
        to_emails=[to_email],
        subject=subject,
        html_content=html,
    )
    message.reply_to = REPLY_TO
    sg = SendGridAPIClient(SENDGRID_API_KEY)
    resp = sg.send(message)
    return resp.status_code

tab1, tab2, tab3, tab4, tab5 = st.tabs(["Dashboard","Scrape","Contacts","Export","Email"])

with tab1:
    st.caption("SQLite DB — works on Streamlit Cloud.")
    with db_conn() as con:
        total = con.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
    st.metric("Contacts in DB", total)

with tab2:
    st.subheader("Search & Extract (South Florida)")
    default_queries = [
        "General Contractors South Florida",
        "Construction Companies Miami-Dade",
        "Architecture Firms South Florida",
        "Flooring Installation Contractors Miami-Dade & Broward",
        "Commercial Flooring Companies Broward",
        "Tile Installation Specialists South Florida",
        "General Contractors Fort Lauderdale",
        "Construction Companies Palm Beach County",
        "Commercial Builders Miami",
        "Architects Broward County"
    ]
    queries = st.text_area("Queries (one per line)", "\n".join(default_queries)).splitlines()
    if st.button("Run search (first 100)"):
        urls = []
        for q in queries:
            try: urls += search_google(q)
            except Exception: pass
            try: urls += search_bing(q)
            except Exception: pass
            try: urls += search_ddg(q)
            except Exception: pass
        seen = {}
        for u in urls:
            d = domain(u) or u
            seen.setdefault(d, u)
        urls = list(seen.values())[:100]
        st.write(f"Unique domains: {len(urls)}")
        for u in urls:
            dom = domain(u)
            if is_competitor(dom): 
                continue
            info = extract_company_info(u)
            info["domain"] = dom
            info["score"] = score_lead(info)
            if info.get("email"):
                upsert_company(info)
        st.success("Search/import complete.")

with tab3:
    st.subheader("Contacts (Top 500 by score)")
    with db_conn() as con:
        df = pd.read_sql_query("SELECT name, email, website, phone, address, score FROM companies ORDER BY score DESC LIMIT 500", con)
    st.dataframe(df)

with tab4:
    st.subheader("CSV Import / Export")
    if st.button("Export CSV"):
        with db_conn() as con:
            df = pd.read_sql_query("SELECT name as 'Company Name', email as 'Primary Contact Email', website as 'Website URL', phone as 'Phone Number', address as 'Business Address', score as 'Lead Score' FROM companies", con)
        st.download_button("Download contacts_export.csv", data=df.to_csv(index=False), file_name="contacts_export.csv", mime="text/csv")
    up = st.file_uploader("Import CSV", type=["csv"])
    if up is not None:
        df = pd.read_csv(up)
        df = df.drop_duplicates(subset=["Primary Contact Email"]).reset_index(drop=True)
        for _, row in df.iterrows():
            rec = {
                "name": str(row.get("Company Name",""))[:255],
                "email": row.get("Primary Contact Email"),
                "website": row.get("Website URL"),
                "phone": row.get("Phone Number"),
                "address": row.get("Business Address"),
                "source": "import",
                "domain": domain(row.get("Website URL") or "")
            }
            if rec["email"]:
                rec["score"] = score_lead(rec)
                upsert_company(rec)
        st.success("Imported CSV.")

with tab5:
    st.subheader("Email (SendGrid)")
    st.caption("Set SENDGRID_API_KEY in Streamlit Secrets.")
    subject = st.text_input("Subject", "Premium Flooring Solutions for Your Projects - Miami Master Flooring")
    body = st.text_area("HTML Body", value=(
        "<p>Dear Team,</p>"
        "<p>At <strong>Miami Master Flooring</strong>, we specialize in high-end flooring installations across South Florida.</p>"
        "<ul><li>Luxury vinyl plank (LVP)</li><li>Waterproof flooring</li><li>Custom tile and stone</li><li>10-year craftsmanship warranty</li></ul>"
        "<p>Would a quick call next week work for you?</p>"
        f"<p>Best regards,<br>{SENDER_NAME}<br>{SENDER_EMAIL}</p>"
    ), height=250)

    with db_conn() as con:
        rows = con.execute("SELECT email FROM companies").fetchall()
        suppressed = set(e[0] for e in con.execute("SELECT email FROM suppression").fetchall())
    candidates = [r[0] for r in rows if r and r[0] and r[0] not in suppressed]
    st.write(f"Eligible recipients: {len(candidates)}")

    to_preview = st.selectbox("Preview recipient", options=candidates[:50] if candidates else ["no-data"])

    daily_cap = st.number_input("Daily send cap", min_value=10, max_value=500, value=150, step=10)
    if st.button("Send test to preview recipient"):
        if to_preview and to_preview != "no-data":
            try:
                code = send_email_via_sendgrid(to_preview, subject, body)
                st.success(f"Sent to {to_preview} (HTTP {code})")
            except Exception as e:
                st.error(str(e))

    if st.button("Send campaign now (up to daily cap)"):
        sent = 0
        for e in candidates:
            if sent >= daily_cap: break
            try:
                send_email_via_sendgrid(e, subject, body)
                sent += 1
                time.sleep(0.3)
            except Exception:
                continue
        st.success(f"Sent {sent} emails.")

    st.divider()
    st.subheader("Suppression (Unsubscribe)")
    unsub_email = st.text_input("Email to suppress")
    if st.button("Add to suppression list"):
        if EMAIL_RE.match(unsub_email or ""):
            with db_conn() as con:
                try:
                    con.execute("INSERT OR IGNORE INTO suppression (email) VALUES (?)", (unsub_email.strip().lower(),))
                    con.commit()
                except Exception:
                    pass
            st.success("Added to suppression list.")
        else:
            st.error("Enter a valid email.")
