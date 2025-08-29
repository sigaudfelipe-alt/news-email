#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Gera e envia uma newsletter diária.
- Lê credenciais via variáveis de ambiente (GitHub Secrets)
- Usa Selenium (Chrome headless) com CHROME_BIN/CHROMEDRIVER_BIN
- Coleta links das seções definidas, resolve URLs relativas, relaxa filtros de título
- Para NYT, tem fallback via RSS se a página HTML não retornar links
- Monta HTML e envia por SMTP
"""

import os
import sys
import time
import smtplib
import traceback
from urllib.parse import urljoin
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
        "Finanças": "https://valor.globo.com/financas/",   # <- garantido
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
    # com e sem acento para não depender de libs extras
    "plano de saude", "plano de saúde",
    "saude", "saúde",
    "seguros", "seguro",
    "health",
    "health insurance",
    "insurance",
}

USER_AGENT = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                            "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"}


def get_driver():
    """Inicializa o Chrome headless pegando caminhos do ambiente do GitHub Actions."""
    chrome_path = os.getenv("CHROME_BIN")
    chromedriver_path = os.getenv("CHROMEDRIVER_BIN", "/usr/bin/chromedriver")

    opts = Options()
    if chrome_path:
        opts.binary_location = chrome_path
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--window-size=1366,900")
    service = Service(chromedriver_path)
    driver = webdriver.Chrome(service=service, options=opts)
    driver.set_page_load_timeout(60)
    return driver


def login_paywall_examples(driver):
    """
    PONTOS DE AJUSTE (opcional):
    Se quiser efetuar login em cada site, implemente aqui as rotinas específicas.
    Credenciais vêm de variáveis de ambiente (GitHub Secrets).
    """
    # Exemplo de esqueleto (comentado):
    # est_user = os.getenv("ESTADAO_USER"); est_pass = os.getenv("ESTADAO_PASS")
    # driver.get("https://accounts.estadao.com.br/login")
    # WebDriverWait(driver, 20).until(
    #     EC.presence_of_element_located((By.CSS_SELECTOR, "input[type=email]"))
    # ).send_keys(est_user)
    # ...
    pass


def fetch_links_simple(driver, url, max_items=6):
    """
    Coleta links/títulos de uma página de seção.
    - Resolve links relativos (urljoin)
    - Relaxa filtros de título (>= 20 chars)
    - Remove duplicados e ignora rotas de login/assinatura/anchors
    """
    driver.get(url)
    WebDriverWait(driver, 25).until(
        EC.presence_of_all_elements_located((By.TAG_NAME, "a"))
    )

    html = driver.page_source
    soup = BeautifulSoup(html, "lxml")

    seen = set()
    items = []
    for a in soup.select("a[href]"):
        title = (a.get_text() or "").strip()
        href = a.get("href")
        if not href or not title:
            continue

        full = urljoin(url, href)  # resolve relativo -> absoluto

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


def download_article_text(url, timeout=25):
    """Baixa conteúdo bruto (sem login) para gerar resumo. Ajuste conforme necessidade."""
    try:
        r = requests.get(url, timeout=timeout, headers=USER_AGENT)
        if r.status_code != 200:
            return ""
        soup = BeautifulSoup(r.text, "lxml")
        # pega parágrafos do corpo comum
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


def normalize_kw(s: str) -> str:
    """Normalização leve (minúsculas + troca simples de acentos comuns)."""
    s = s.lower()
    # trocas mínimas para 'saúde'/'saude'
    return (s.replace("á", "a").replace("à", "a").replace("â", "a")
             .replace("é", "e").replace("ê", "e")
             .replace("í", "i")
             .replace("ó", "o").replace("ô", "o")
             .replace("ú", "u").replace("ç", "c"))


def build_html(news_per_source, health_items):
    """Monta HTML final da newsletter."""
    html = []
    html.append("<html><body style='font-family:Arial,Helvetica,sans-serif'>")
    html.append("<h2>Resumo Diário – 07:00</h2>")

    for source, blocks in news_per_source.items():
        if not blocks:
            continue
        html.append(f"<h3>{source}</h3>")
        for section, items in blocks:
            if not items:
                continue
            html.append(f"<h4>{section}</h4><ul>")
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

    # SMTP Gmail padrão; ajuste se usar outro provedor
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as srv:
        srv.login(remetente, senha)
        srv.send_message(msg)


def rotina():
    driver = get_driver()
    try:
        login_paywall_examples(driver)  # opcional, implemente se quiser login

        news_per_source = {}
        health_bucket = []

        for fonte, sections in SECTIONS.items():
            collected = []
            for section_name, url in sections.items():
                links = fetch_links_simple(driver, url, max_items=6)

                # Fallback só para NYT se nada veio via HTML
                if fonte == "NYT" and not links and section_name in NYT_RSS:
                    links = fetch_rss_fallback(NYT_RSS[section_name], max_items=6)

                enriched = []
                for title, link in links:
                    text = download_article_text(link)
                    summary = summarize_text(text)
                    enriched.append((title, link, summary))

                    # bucket especial de saúde/seguros
                    t_norm = normalize_kw(title)
                    if any(normalize_kw(k) in t_norm for k in HEALTH_KEYWORDS):
                        health_bucket.append((title, link, summary))

                collected.append((section_name, enriched))
            news_per_source[fonte] = collected

        html = build_html(news_per_source, health_bucket)
        enviar_email(html)

    finally:
        driver.quit()


if __name__ == "__main__":
    try:
        # quando rodar localmente com .env (no GitHub Actions basta Secrets)
        if os.path.exists(".env"):
            load_dotenv()
        rotina()
    except Exception as e:
        traceback.print_exc()
        sys.exit(1)
