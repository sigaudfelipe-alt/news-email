#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Newsletter diária (07:00 America/Sao_Paulo)
- Estrutura por JORNAL → SEÇÃO (bullets com resumo) no padrão combinado
- Especial Saúde/Planos/Seguros
- Envia somente às 07:00 BRT (a menos que FORCE_SEND_ANYTIME=1)
- Coleta por editoria correta (filtro por path)
- NYT via RSS com description/content como base do resumo
- Economia com bloco "Como ler" (explicação)
- Deduplicação global por URL canônica + similaridade de título
- requests -> fallback Selenium (headless) quando necessário
- SMTP com debug, SSL→STARTTLS, 3 tentativas e variáveis para host/port
"""

import os
import sys
import re
import smtplib
import traceback
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv  # para rodar localmente
from zoneinfo import ZoneInfo

# ====== Selenium (fallback) ======
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# ----------------- Configurações por jornal/ seção -----------------

SECTIONS = {
    "Estadão": {
        "Política": {
            "url": "https://www.estadao.com.br/politica/",
            "path_must_include": ["/politica/"],
        },
        "Economia": {
            "url": "https://economia.estadao.com.br/",
            "path_must_include": ["/economia/"],
        },
    },
    "Valor": {
        "Primeiro Caderno": {
            "url": "https://valor.globo.com/brasil/",
            "path_must_include": ["/brasil/"],
        },
        "Empresas": {
            "url": "https://valor.globo.com/empresas/",
            "path_must_include": ["/empresas/"],
        },
        "Finanças": {
            "url": "https://valor.globo.com/financas/",
            "path_must_include": ["/financas/"],
        },
    },
    "O Globo": {
        "Primeiro Caderno": {
            "url": "https://oglobo.globo.com/brasil/",
            "path_must_include": ["/brasil/"],
        },
    },
    "NYT": {
        "General":   {"rss": "https://rss.nytimes.com/services/xml/rss/nyt/World.xml"},
        "Business":  {"rss": "https://rss.nytimes.com/services/xml/rss/nyt/Business.xml"},
        "Finance":   {"rss": "https://rss.nytimes.com/services/xml/rss/nyt/Economy.xml"},
        "Opinion":   {"rss": "https://rss.nytimes.com/services/xml/rss/nyt/Opinion.xml"},
    },
}

HEALTH_KEYWORDS = {
    "plano de saude", "plano de saúde",
    "saude", "saúde",
    "seguros", "seguro",
    "health", "health insurance", "insurance",
}

USER_AGENT = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
    )
}

WANT_PER_SECTION = 4     # alvo por editoria
SCAN_LIMIT = 60          # quantos links brutos vasculhar por seção
TITLE_SIM_THRESHOLD = 0.85

# ----------------- Utilidades de horário (07:00 BRT) -----------------

def should_send_now():
    if os.getenv("FORCE_SEND_ANYTIME") == "1":
        return True
    now = datetime.utcnow().replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo("America/Sao_Paulo"))
    return now.hour == 7  # envia apenas às 07:00

# ----------------- Chrome headless estável no CI -----------------

def get_driver():
    chrome_path = os.getenv("CHROME_BIN")
    chromedriver_path = os.getenv("CHROMEDRIVER_BIN", "/usr/bin/chromedriver")

    opts = Options()
    if chrome_path:
        opts.binary_location = chrome_path

    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--window-size=1366,900")
    opts.add_argument("--disable-renderer-backgrounding")
    opts.add_argument("--disable-background-timer-throttling")
    opts.add_argument("--disable-backgrounding-occluded-windows")
    opts.add_argument("--disable-features=TranslateUI,BlinkGenPropertyTrees,AutomationControlled")
    opts.add_argument("--remote-debugging-port=9222")
    opts.page_load_strategy = "eager"
    prefs = {
        "profile.managed_default_content_settings.images": 2,
        "profile.default_content_setting_values.cookies": 1,
        "disk-cache-size": 4096,
    }
    opts.add_experimental_option("prefs", prefs)

    service = Service(chromedriver_path)
    driver = webdriver.Chrome(service=service, options=opts)
    driver.set_page_load_timeout(90)
    driver.implicitly_wait(0)
    return driver

# ----------------- Normalização / deduplicação -----------------

STOPWORDS = {"de","da","do","das","dos","para","por","em","no","na","nos","nas",
             "e","a","o","as","os","um","uma","ao","à","com","sobre","contra","entre",
             "se","que","porém","mas","ou"}

def normalize_kw(s: str) -> str:
    s = s.lower()
    return (s.replace("á", "a").replace("à", "a").replace("â", "a")
             .replace("é", "e").replace("ê", "e")
             .replace("í", "i")
             .replace("ó", "o").replace("ô", "o")
             .replace("ú", "u").replace("ç", "c"))

def normalize_url(u: str) -> str:
    try:
        s = urlsplit(u.strip())
        host = (s.netloc or "").lower()
        if host.startswith("www."):
            host = host[4:]
        path = (s.path or "").rstrip("/")
        return urlunsplit(("", host, path, "", ""))
    except Exception:
        return u

def tokenize_title(t: str):
    t = normalize_kw(t)
    t = re.sub(r"[^a-z0-9\s]", " ", t)
    tokens = [tok for tok in t.split() if tok and tok not in STOPWORDS]
    return set(tokens)

def title_similarity(t1: str, t2: str) -> float:
    a, b = tokenize_title(t1), tokenize_title(t2)
    if not a or not b:
        return 0.0
    inter = len(a & b)
    uni = len(a | b)
    return inter / uni

class Deduper:
    def __init__(self, title_threshold: float = TITLE_SIM_THRESHOLD):
        self.seen_urls = set()
        self.titles = []

    def is_dup(self, title: str, url: str) -> bool:
        u_norm = normalize_url(url)
        if u_norm in self.seen_urls:
            return True
        for t in self.titles:
            if title_similarity(t, title) >= TITLE_SIM_THRESHOLD:
                return True
        self.seen_urls.add(u_norm)
        self.titles.append(title)
        return False

# ----------------- Coleta (requests → fallback Selenium) -----------------

def _filter_links(url_base, soup, scan_limit=SCAN_LIMIT):
    seen, items = set(), []
    for a in soup.select("a[href]"):
        title = (a.get_text() or "").strip()
        href = a.get("href")
        if not href or not title:
            continue
        full = urljoin(url_base, href)
        if len(title) < 20:
            continue
        if any(x in full for x in ("/subscribe", "/signin", "/login", "#")):
            continue
        if full not in seen:
            seen.add(full)
            items.append((title, full))
            if len(items) >= scan_limit:
                break
    return items

def fetch_links_via_requests(url, scan_limit=SCAN_LIMIT):
    try:
        r = requests.get(url, timeout=20, headers=USER_AGENT)
        if r.status_code != 200:
            return []
        soup = BeautifulSoup(r.text, "lxml")
        return _filter_links(url, soup, scan_limit=scan_limit)
    except Exception:
        return []

def fetch_links_via_selenium(driver, url, scan_limit=SCAN_LIMIT):
    try:
        driver.get(url)
        WebDriverWait(driver, 15).until(EC.presence_of_element_located((By.TAG_NAME, "a")))
        soup = BeautifulSoup(driver.page_source, "lxml")
        return _filter_links(url, soup, scan_limit=scan_limit)
    except Exception:
        return []

def fetch_links_bulk(driver, url, scan_limit=SCAN_LIMIT):
    links = fetch_links_via_requests(url, scan_limit=scan_limit)
    if links:
        return links
    return fetch_links_via_selenium(driver, url, scan_limit=scan_limit)

def belongs_to_section(url: str, must_parts: list[str]) -> bool:
    path = urlsplit(url).path.lower()
    return any(part in path for part in must_parts)

# ----------------- Download + resumo -----------------

def download_article_text(url, timeout=25):
    try:
        r = requests.get(url, timeout=timeout, headers=USER_AGENT)
        if r.status_code != 200:
            return ""
        soup = BeautifulSoup(r.text, "lxml")
        paras = [p.get_text(" ", strip=True) for p in soup.select("article p")]
        if not paras:
            paras = [p.get_text(" ", strip=True) for p in soup.select("p")]
        text = " ".join(paras)
        return text[:12000]
    except Exception:
        return ""

def summarize_text(text, max_chars=900):
    if not text:
        return ""
    parts = [p.strip() for p in text.split(". ") if len(p.strip()) > 40]
    summary = ". ".join(parts[:6])
    if len(summary) > max_chars:
        summary = summary[:max_chars].rsplit(" ", 1)[0] + "…"
    return summary

# ----------------- Explicador para Economia -----------------

def economic_explainer(text_or_title: str) -> str:
    t = normalize_kw(text_or_title)
    hints = []
    if any(k in t for k in ["selic","juros","taxa de juros","copom"]):
        hints.append("Juros altos encarecem crédito e tendem a desacelerar consumo e investimento.")
    if any(k in t for k in ["inflacao","ipca","precos"]):
        hints.append("Inflação alta corrói renda real e reduz poder de compra das famílias.")
    if any(k in t for k in ["pib","atividade","crescimento"]):
        hints.append("PIB fraco indica demanda desaquecida; setores cíclicos sentem primeiro.")
    if any(k in t for k in ["fiscal","arcabouco","deficit","divida","primario"]):
        hints.append("Risco fiscal pressiona juros longos e pode impor cortes de gasto/alta de impostos.")
    if any(k in t for k in ["emprego","desemprego","mercado de trabalho"]):
        hints.append("Mercado de trabalho fraco costuma atrasar recuperação do consumo.")
    if any(k in t for k in ["credito","inadimplencia","calote"]):
        hints.append("Crédito restrito e inadimplência alta restringem vendas e investimento.")
    if not hints:
        hints.append("Acompanhe impactos sobre juros, inflação, emprego e contas públicas para contexto.")
    return " ".join(hints[:2])

# ----------------- NYT via RSS com description/content -----------------

def fetch_nyt_rss(feed_url, max_items=WANT_PER_SECTION*2):
    out = []
    try:
        r = requests.get(feed_url, timeout=20, headers=USER_AGENT)
        if r.status_code != 200:
            return out
        soup = BeautifulSoup(r.text, "xml")
        for item in soup.select("item")[:max_items]:
            title = item.title.get_text(strip=True) if item.title else ""
            link = item.link.get_text(strip=True) if item.link else ""
            desc = ""
            if item.find("description"):
                desc = BeautifulSoup(item.description.get_text(), "lxml").get_text(" ", strip=True)
            content_tag = item.find("content:encoded")
            if not desc and content_tag:
                desc = BeautifulSoup(content_tag.get_text(), "lxml").get_text(" ", strip=True)
            if not desc and link:
                desc = summarize_text(download_article_text(link))
            if title and link:
                out.append((title, link, summarize_text(desc)))
        return out
    except Exception:
        return out

# ----------------- Montagem da newsletter (padrão acordado) -----------------

def build_html(news_per_source, health_items):
    html = []
    html.append("<html><body style='font-family:Arial,Helvetica,sans-serif'>")
    html.append("<h2>Resumo diário – 07:00</h2>")

    ordem = ["Estadão", "Valor", "O Globo", "NYT"]
    for jornal in ordem:
        blocks = news_per_source.get(jornal, [])
        if not blocks:
            continue
        if jornal == "Estadão":
            html.append("<h3>Política e Economia – O Estado de S. Paulo</h3>")
        elif jornal == "Valor":
            html.append("<h3>Economia & Finanças – Valor Econômico</h3>")
        elif jornal == "O Globo":
            html.append("<h3>Primeiro Caderno – O Globo</h3>")
        elif jornal == "NYT":
            html.append("<h3>The New York Times – Geral / Business / Finance / Opinion</h3>")

        for section_name, items in blocks:
            if not items:
                continue
            html.append(f"<h4>{section_name}</h4>")
            html.append("<ul>")
            for title, link, summary, is_econ in items:
                html.append("<li>")
                html.append(f"<p><strong><a href='{link}'>{title}</a></strong><br>{summary}</p>")
                if is_econ:
                    html.append(f"<p><em>Como ler:</em> {economic_explainer(title + ' ' + summary)}</p>")
                html.append("</li>")
            html.append("</ul>")

    if health_items:
        html.append("<hr>")
        html.append("<h3>Especial: Saúde / Planos / Seguros</h3>")
        html.append("<ul>")
        for title, link, summary in health_items:
            html.append(f"<li><p><strong><a href='{link}'>{title}</a></strong><br>{summary}</p></li>")
        html.append("</ul>")

    html.append("</body></html>")
    return "\n".join(html)

# ----------------- E-mail (debug + SSL→STARTTLS + retries) -----------------

def enviar_email(conteudo_html):
    """
    Envia o HTML por SMTP usando credenciais do ambiente (secrets).
    - Tenta primeiro SSL (porta 465), depois STARTTLS (porta 587)
    - Faz até 3 tentativas com pequeno backoff
    - Loga detalhes úteis no CI (sem expor segredos)
    É possível sobrescrever host/portas via env:
        EMAIL_SMTP_HOST, EMAIL_SMTP_SSL_PORT, EMAIL_SMTP_TLS_PORT
    """
    remetente = os.getenv("EMAIL_USER")
    senha = os.getenv("EMAIL_PASS")
    destinatario = os.getenv("DEST_EMAIL")
    if not (remetente and senha and destinatario):
        raise RuntimeError("EMAIL_USER/EMAIL_PASS/DEST_EMAIL não configurados.")

    host = os.getenv("EMAIL_SMTP_HOST", "smtp.gmail.com")
    ssl_port = int(os.getenv("EMAIL_SMTP_SSL_PORT", "465"))
    tls_port = int(os.getenv("EMAIL_SMTP_TLS_PORT", "587"))

    msg = MIMEMultipart("alternative")
    msg["Subject"] = "Resumo diário de notícias"
    msg["From"] = remetente
    msg["To"] = destinatario
    msg["Reply-To"] = remetente
    msg.attach(MIMEText(conteudo_html, "html", "utf-8"))

    def _try_ssl():
        import smtplib
        with smtplib.SMTP_SSL(host, ssl_port, timeout=30) as srv:
            srv.set_debuglevel(1)  # imprime conversa SMTP no log do Actions
            srv.login(remetente, senha)
            return srv.send_message(msg)

    def _try_tls():
        import smtplib
        with smtplib.SMTP(host, tls_port, timeout=30) as srv:
            srv.set_debuglevel(1)
            srv.ehlo()
            srv.starttls()
            srv.ehlo()
            srv.login(remetente, senha)
            return srv.send_message(msg)

    last_err = None
    for attempt in range(1, 4):
        try:
            print(f"[mail] Tentativa {attempt}/3 via SSL {host}:{ssl_port} -> To={destinatario}")
            _try_ssl()
            print("[mail] Enviado com SSL.")
            return
        except Exception as e_ssl:
            print(f"[mail] Falha SSL: {e_ssl!r}")
            last_err = e_ssl
            try:
                print(f"[mail] Tentativa {attempt}/3 via STARTTLS {host}:{tls_port} -> To={destinatario}")
                _try_tls()
                print("[mail] Enviado com STARTTLS.")
                return
            except Exception as e_tls:
                print(f"[mail] Falha STARTTLS: {e_tls!r}")
                last_err = e_tls
                import time
                time.sleep(2 * attempt)

    raise RuntimeError(f"Falha ao enviar e-mail após 3 tentativas: {last_err!r}")

# ----------------- Coleta por seção com filtro de editoria + dedup -----------------

def collect_section_items(driver, url, must_parts, global_deduper, want_items=WANT_PER_SECTION):
    raw_links = fetch_links_bulk(driver, url, scan_limit=SCAN_LIMIT)
    section_items = []
    for title, link in raw_links:
        if not belongs_to_section(link, must_parts):
            continue
        if global_deduper.is_dup(title, link):
            continue
        text = download_article_text(link)
        summary = summarize_text(text)
        section_items.append((title, link, summary))
        if len(section_items) >= want_items:
            break
    return section_items

# ----------------- Rotina principal -----------------

def rotina():
    if not should_send_now():
        print("Agora não é 07:00 America/Sao_Paulo (use FORCE_SEND_ANYTIME=1 para forçar).")
        return

    driver = get_driver()
    try:
        news_per_source = {}
        health_bucket = []
        deduper = Deduper()

        for jornal, sections in SECTIONS.items():
            collected = []

            # NYT via RSS (resumos mais consistentes)
            if jornal == "NYT":
                for section_name, conf in sections.items():
                    rss = conf.get("rss")
                    if not rss:
                        continue
                    items = []
                    for (title, link, summary) in fetch_nyt_rss(rss, max_items=WANT_PER_SECTION * 2):
                        if deduper.is_dup(title, link):
                            continue
                        is_econ = section_name.lower() in {"finance", "business"}
                        items.append((title, link, summary, is_econ))
                        # bucket de saúde
                        t_norm = normalize_kw(title)
                        if any(normalize_kw(k) in t_norm for k in HEALTH_KEYWORDS):
                            health_bucket.append((title, link, summary))
                        if len(items) >= WANT_PER_SECTION:
                            break
                    collected.append((section_name, items))
                news_per_source[jornal] = collected
                continue

            # Demais jornais (HTML com filtro por editoria)
            for section_name, conf in sections.items():
                url = conf["url"]
                must_parts = conf["path_must_include"]
                items = []
                for (title, link, summary) in collect_section_items(
                        driver, url, must_parts, deduper, want_items=WANT_PER_SECTION):
                    is_econ = (section_name.lower() in {"economia", "finanças", "empresas"})
                    items.append((title, link, summary, is_econ))

                    # bucket de saúde
                    t_norm = normalize_kw(title)
                    if any(normalize_kw(k) in t_norm for k in HEALTH_KEYWORDS):
                        health_bucket.append((title, link, summary))

                collected.append((section_name, items))
            news_per_source[jornal] = collected

        html = build_html(news_per_source, health_bucket)
        enviar_email(html)

    finally:
        driver.quit()

# ----------------- Main -----------------

if __name__ == "__main__":
    try:
        if os.path.exists(".env"):
            load_dotenv()  # útil localmente
        rotina()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
