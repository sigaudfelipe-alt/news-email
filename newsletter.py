#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Gera e envia uma newsletter diária.
- Lê credenciais via variáveis de ambiente (GitHub Secrets)
- Usa Selenium (Chrome headless) com CHROME_BIN/CHROMEDRIVER_BIN
- Coleta links das seções definidas (requests -> fallback Selenium)
- Resolve URLs relativas, relaxa filtros de título
- Dedup global por URL canônica e similaridade de título
- Garante população por SEÇÃO (tenta buscar mais itens se dedup esvaziar)
- Para NYT, fallback via RSS se a página HTML não retornar links
- Monta HTML e envia por SMTP
"""

import os
import sys
import re
import smtplib
import traceback
from urllib.parse import urljoin, urlsplit, urlunsplit
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv  # opcional para rodar localmente

# ====== Selenium (Chrome Headless) ======
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# -------- Seções por jornal --------
SECTIONS = {
    "Estadão": {
        "Política": "https://www.estadao.com.br/politica/",
        "Economia": "https://economia.estadao.com.br/",
    },
    "Valor": {
        "Primeiro Caderno": "https://valor.globo.com/brasil/",
        "Empresas": "https://valor.globo.com/empresas/",
        "Finanças": "https://valor.globo.com/financas/",
    },
    "O Globo": {
        "Primeiro Caderno": "https://oglobo.globo.com/brasil/",
    },
    "NYT": {
        "General": "https://www.nytimes.com/section/world",
        "Business": "https://www.nytimes.com/section/business",
        "Finance": "https://www.nytimes.com/section/business/economy",
        "Opinion": "https://www.nytimes.com/section/opinion",
    },
}

# Fallback RSS para NYT
NYT_RSS = {
    "General": "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",
    "Business": "https://rss.nytimes.com/services/xml/rss/nyt/Business.xml",
    "Finance": "https://rss.nytimes.com/services/xml/rss/nyt/Economy.xml",
    "Opinion": "https://rss.nytimes.com/services/xml/rss/nyt/Opinion.xml",
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

# ===================== Driver Chrome (estável no CI) =====================

def get_driver():
    """Inicializa o Chrome headless pegando caminhos do ambiente do GitHub Actions."""
    chrome_path = os.getenv("CHROME_BIN")
    chromedriver_path = os.getenv("CHROMEDRIVER_BIN", "/usr/bin/chromedriver")

    opts = Options()
    if chrome_path:
        opts.binary_location = chrome_path

    # Flags para estabilidade/performance em ambiente CI
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

    # Bloquear imagens para carregar mais rápido
    prefs = {
        "profile.managed_default_content_settings.images": 2,
        "profile.default_content_setting_values.cookies": 1,
        "disk-cache-size": 4096,
    }
    opts.add_experimental_option("prefs", prefs)

    # Não esperar todos os recursos pesados
    opts.page_load_strategy = "eager"

    service = Service(chromedriver_path)
    driver = webdriver.Chrome(service=service, options=opts)
    driver.set_page_load_timeout(90)
    driver.implicitly_wait(0)
    return driver

# ===================== Login (opcional; ajustar se quiser) =====================

def login_paywall_examples(driver):
    """Implemente aqui se quiser login nas contas de assinante (opcional)."""
    pass

# ===================== Coleta de links (requests -> fallback Selenium) =====================

def _filter_links(url_base, soup, max_items=6):
    """Filtra e normaliza anchors para (title, full_url)."""
    seen, items = set(), []
    for a in soup.select("a[href]"):
        title = (a.get_text() or "").strip()
        href = a.get("href")
        if not href or not title:
            continue
        full = urljoin(url_base, href)  # resolve relativo -> absoluto

        # filtros básicos
        if len(title) < 20:
            continue
        if any(x in full for x in ("/subscribe", "/signin", "/login", "#")):
            continue

        if full not in seen:
            seen.add(full)
            items.append((title, full))
            if len(items) >= max_items:
                break
    return items

def fetch_links_via_requests(url, scan_limit=40):
    """Primeira tentativa: pegar links via requests+BS (sem renderer)."""
    try:
        r = requests.get(url, timeout=20, headers=USER_AGENT)
        if r.status_code != 200:
            return []
        soup = BeautifulSoup(r.text, "lxml")
        # coletar mais do que o necessário (scan_limit) para repor após dedup
        return _filter_links(url, soup, max_items=scan_limit)
    except Exception:
        return []

def fetch_links_via_selenium(driver, url, scan_limit=40):
    """Segunda tentativa: Selenium (casos mais dinâmicos)."""
    try:
        driver.get(url)
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.TAG_NAME, "a"))
        )
        html = driver.page_source
        soup = BeautifulSoup(html, "lxml")
        return _filter_links(url, soup, max_items=scan_limit)
    except Exception:
        return []

def fetch_links_bulk(driver, url, scan_limit=40):
    """
    Wrapper: tenta requests antes; se vazio, Selenium.
    Retorna uma lista "longa" (scan_limit) para permitir reposição pós-dedup.
    """
    links = fetch_links_via_requests(url, scan_limit=scan_limit)
    if links:
        return links
    return fetch_links_via_selenium(driver, url, scan_limit=scan_limit)

# ===================== Download + resumo =====================

def download_article_text(url, timeout=25):
    """Baixa conteúdo bruto (sem login) para gerar resumo. Ajuste conforme necessidade."""
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
    """Resumo simples, robusto para CI (sem modelos pesados)."""
    if not text:
        return ""
    parts = [p.strip() for p in text.split(". ") if len(p.strip()) > 40]
    summary = ". ".join(parts[:6])
    if len(summary) > max_chars:
        summary = summary[:max_chars].rsplit(" ", 1)[0] + "…"
    return summary

# ===================== Normalização / deduplicação =====================

STOPWORDS = {"de","da","do","das","dos","para","por","em","no","na","nos","nas",
             "e","a","o","as","os","um","uma","ao","à","com","sobre","contra","entre",
             "se","que","porém","mas","ou"}

def normalize_kw(s: str) -> str:
    """Normalização leve (minúsculas + troca simples de acentos comuns)."""
    s = s.lower()
    return (s.replace("á", "a").replace("à", "a").replace("â", "a")
             .replace("é", "e").replace("ê", "e")
             .replace("í", "i")
             .replace("ó", "o").replace("ô", "o")
             .replace("ú", "u").replace("ç", "c"))

def normalize_url(u: str) -> str:
    """URL canônica simples (remove schema/params/fragments, normaliza host)."""
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
    """Similaridade Jaccard simples entre conjuntos de tokens (0..1)."""
    a, b = tokenize_title(t1), tokenize_title(t2)
    if not a or not b:
        return 0.0
    inter = len(a & b)
    uni = len(a | b)
    return inter / uni

class Deduper:
    def __init__(self, title_threshold: float = 0.85):
        self.seen_urls = set()
        self.titles = []  # títulos aceitos
        self.title_threshold = title_threshold

    def is_dup(self, title: str, url: str) -> bool:
        u_norm = normalize_url(url)
        if u_norm in self.seen_urls:
            return True
        for t in self.titles:
            if title_similarity(t, title) >= self.title_threshold:
                return True
        # registra como novo
        self.seen_urls.add(u_norm)
        self.titles.append(title)
        return False

# ===================== Montagem da newsletter =====================

def build_html(news_per_source, health_items, show_empty_sections=True):
    """Monta HTML final da newsletter."""
    html = []
    html.append("<html><body style='font-family:Arial,Helvetica,sans-serif'>")
    html.append("<h2>Resumo Diário – 07:00</h2>")

    for source, blocks in news_per_source.items():
        html.append(f"<h3>{source}</h3>")
        for section, items in blocks:
            html.append(f"<h4>{section}</h4>")
            if not items:
                if show_empty_sections:
                    html.append("<p><em>(sem novidades distintas da editoria principal hoje)</em></p>")
                else:
                    html.append("<p>&nbsp;</p>")
                continue
            html.append("<ul>")
            for title, link, summary in items:
                html.append(
                    f"<li><p><strong><a href='{link}'>{title}</a></strong><br>"
                    f"{summary}</p></li>"
                )
            html.append("</ul>")

    if health_items:
        html.append("<hr><h3>Especial: Saúde / Planos / Seguros</h3><ul>")
        for title, link, summary in health_items:
            html.append(
                f"<li><p><strong><a href='{link}'>"
                f"{title}</a></strong><br>{summary}</p></li>"
            )
        html.append("</ul>")

    html.append("</body></html>")
    return "\n".join(html)

def enviar_email(conteudo_html):
    """Envia o HTML por SMTP usando credenciais do ambiente (secrets)."""
    remetente = os.getenv("EMAIL_USER")
    senha = os.getenv("EMAIL_PASS")
    destinatario = os.getenv("DEST_EMAIL")
    if not (remetente and senha and destinatario):
        raise RuntimeError("EMAIL_USER/EMAIL_PASS/DEST_EMAIL não configurados.")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = "Resumo diário de notícias"
    msg["From"] = remetente
    msg["To"] = destinatario
    msg.attach(MIMEText(conteudo_html, "html", "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as srv:
        srv.login(remetente, senha)
        srv.send_message(msg)

# ===================== Coleta por seção com REPOSIÇÃO =====================

def collect_section_items(driver, url, global_deduper, want_items=4, scan_limit=40):
    """
    Busca links da seção (scan_limit bem maior que want_items) e
    adiciona ao resultado apenas aqueles que não sejam duplicados globais.
    Se muitos forem descartados pelo dedup, tenta preencher até want_items.
    """
    raw_links = fetch_links_bulk(driver, url, scan_limit=scan_limit)
    section_items = []
    for title, link in raw_links:
        if global_deduper.is_dup(title, link):
            continue
        # Aceitou → enriquecer
        text = download_article_text(link)
        summary = summarize_text(text)
        section_items.append((title, link, summary))
        if len(section_items) >= want_items:
            break
    return section_items

# ===================== Rotina principal =====================

def fetch_rss_fallback(feed_url, max_items=6):
    try:
        r = requests.get(feed_url, timeout=20, headers=USER_AGENT)
        if r.status_code != 200:
            return []
        soup = BeautifulSoup(r.text, "xml")
        out = []
        for item in soup.select("item")[:max_items]:
            title = item.title.get_text(strip=True) if item.title else ""
            link = item.link.get_text(strip=True) if item.link else ""
            if title and link:
                out.append((title, link))
        return out
    except Exception:
        return []

def rotina():
    driver = get_driver()
    try:
        login_paywall_examples(driver)  # opcional

        news_per_source = {}
        health_bucket = []
        deduper = Deduper(title_threshold=0.85)

        # Quantidade alvo por seção (ajuste se quiser)
        WANT_PER_SECTION = 4
        SCAN_LIMIT = 40  # quantos links brutos escanear por seção

        for fonte, sections in SECTIONS.items():
            collected = []
            for section_name, url in sections.items():
                # Coleta com reposição (respeita dedup global, mas tenta preencher a seção)
                items = collect_section_items(
                    driver, url, deduper, want_items=WANT_PER_SECTION, scan_limit=SCAN_LIMIT
                )

                # Fallback NYT via RSS se seção ficou vazia
                if fonte == "NYT" and not items and section_name in NYT_RSS:
                    rss_links = fetch_rss_fallback(NYT_RSS[section_name], max_items=WANT_PER_SECTION * 2)
                    for title, link in rss_links:
                        if deduper.is_dup(title, link):
                            continue
                        text = download_article_text(link)
                        summary = summarize_text(text)
                        items.append((title, link, summary))
                        if len(items) >= WANT_PER_SECTION:
                            break

                # Alimenta bucket especial de saúde/seguros a partir dos itens da seção
                for title, link, summary in items:
                    t_norm = normalize_kw(title)
                    if any(normalize_kw(k) in t_norm for k in HEALTH_KEYWORDS):
                        health_bucket.append((title, link, summary))

                collected.append((section_name, items))

            news_per_source[fonte] = collected

        html = build_html(news_per_source, health_bucket, show_empty_sections=True)
        enviar_email(html)

    finally:
        driver.quit()

if __name__ == "__main__":
    try:
        # quando rodar localmente com .env (no GitHub Actions basta Secrets)
        if os.path.exists(".env"):
            load_dotenv()
        rotina()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
