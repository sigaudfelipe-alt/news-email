"""
newsletter.py
===============

This script automates the generation of a daily newsletter from
subscription‑based newspapers and sends it via e‑mail.  It is designed to
run headlessly using Selenium to log into each newspaper with credentials
stored in environment variables, extract articles from specific
sections, summarise their content, build a single HTML newsletter, and
deliver it by SMTP.  Scheduling is left to the caller (for example via
cron or Windows Task Scheduler), but an example using the ``schedule``
library is provided at the end of this file.

The script is deliberately modular: each function has a clearly defined
responsibility, making it straightforward to modify the scraping logic
should the layout of a newspaper change or a new source be added.  To
use this script you must create a `.env` file (see ``.env.example``)
with your login credentials for each newspaper and your e‑mail
configuration.

IMPORTANT:  This code serves as a starting point.  Because the layout
and authentication flows of news websites evolve, you will likely need
to update the CSS selectors and URLs in the scraping functions.  Always
respect the terms of service of the websites you access.

"""

import os
import smtplib
import time
from dataclasses import dataclass
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import List, Tuple

from bs4 import BeautifulSoup
from dotenv import load_dotenv
from selenium import webdriver
from selenium.common.exceptions import NoSuchElementException, TimeoutException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

# Optional summarisation: transformers can be heavyweight, so import
# lazily inside the summarisation function to avoid long start‑up times
try:
    from transformers import pipeline
except ImportError:
    pipeline = None  # summarisation will fall back to simple slicing


@dataclass
class Article:
    """Simple data container for a news article."""
    source: str
    title: str
    url: str
    summary: str


def init_driver() -> webdriver.Chrome:
    """
    Instantiate a headless Chrome WebDriver.

    Returns
    -------
    webdriver.Chrome
        A configured Selenium WebDriver instance.
    """
    chrome_options = Options()
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--disable‑gpu")
    chrome_options.add_argument("--no‑sandbox")
    chrome_options.add_argument("--window‑size=1920,1080")
    driver = webdriver.Chrome(options=chrome_options)
    # Increase default implicit wait to handle dynamic pages
    driver.implicitly_wait(10)
    return driver


def load_credentials() -> dict:
    """
    Load login and e‑mail credentials from environment variables using
    python‑dotenv.  See `.env.example` for variable names.

    Returns
    -------
    dict
        A mapping of credential names to their values.
    """
    load_dotenv()
    creds = {
        "estadao_user": os.getenv("ESTADAO_USER"),
        "estadao_pass": os.getenv("ESTADAO_PASS"),
        "valor_user": os.getenv("VALOR_USER"),
        "valor_pass": os.getenv("VALOR_PASS"),
        "oglobo_user": os.getenv("OGLOBO_USER"),
        "oglobo_pass": os.getenv("OGLOBO_PASS"),
        "nyt_user": os.getenv("NYT_USER"),
        "nyt_pass": os.getenv("NYT_PASS"),
        "email_user": os.getenv("EMAIL_USER"),
        "email_pass": os.getenv("EMAIL_PASS"),
        "dest_email": os.getenv("DEST_EMAIL"),
    }
    # Validate required fields
    missing = [k for k, v in creds.items() if not v]
    if missing:
        raise RuntimeError(
            f"Missing required environment variables: {', '.join(missing)}. "
            "Ensure you have created a .env file with the necessary credentials."
        )
    return creds


def login_with_credentials(
    driver: webdriver.Chrome, login_url: str, user: str, password: str, selectors: dict
) -> None:
    """
    Generic login helper.  Navigates to ``login_url``, fills in the
    username and password fields, and submits the login form.

    Parameters
    ----------
    driver : webdriver.Chrome
        Selenium WebDriver instance.
    login_url : str
        URL of the login page.
    user : str
        Username/email for login.
    password : str
        Password for login.
    selectors : dict
        Dictionary containing CSS selectors or XPath expressions for
        locating the username field, password field, and submit button.
        Expected keys: ``username``, ``password``, ``submit``.
    """
    driver.get(login_url)
    try:
        user_field = driver.find_element(By.CSS_SELECTOR, selectors["username"])
        pass_field = driver.find_element(By.CSS_SELECTOR, selectors["password"])
        submit_button = driver.find_element(By.CSS_SELECTOR, selectors["submit"])
    except NoSuchElementException:
        raise RuntimeError(
            f"Could not find login fields using selectors {selectors} on {login_url}."
        )
    user_field.clear()
    user_field.send_keys(user)
    pass_field.clear()
    pass_field.send_keys(password)
    submit_button.click()
    # Wait a short while for login to complete (adjust as needed)
    time.sleep(5)


def extract_links(driver: webdriver.Chrome, section_url: str, link_selector: str) -> List[Tuple[str, str]]:
    """
    Collect article titles and URLs from a section page.

    Parameters
    ----------
    driver : webdriver.Chrome
        WebDriver instance (must be authenticated if the section requires
        login).
    section_url : str
        URL of the section page to scrape.
    link_selector : str
        CSS selector identifying the anchor elements linking to articles.

    Returns
    -------
    List[Tuple[str, str]]
        A list of (title, url) tuples.
    """
    driver.get(section_url)
    time.sleep(2)
    elements = driver.find_elements(By.CSS_SELECTOR, link_selector)
    articles: List[Tuple[str, str]] = []
    for elem in elements:
        title = elem.text.strip()
        url = elem.get_attribute("href")
        if title and url:
            articles.append((title, url))
    return articles


def extract_article_text(driver: webdriver.Chrome, url: str) -> str:
    """
    Navigate to an article and extract its text content.  Uses
    BeautifulSoup to parse the page source after it has loaded.

    Parameters
    ----------
    driver : webdriver.Chrome
        The active WebDriver instance.
    url : str
        Article URL to visit.

    Returns
    -------
    str
        The plain text content of the article.
    """
    driver.get(url)
    time.sleep(3)
    soup = BeautifulSoup(driver.page_source, "lxml")
    # This CSS selector may need adjusting per site; here we attempt to
    # capture all paragraph elements within the article body.
    paragraphs = soup.select("article p") or soup.select("div.content p")
    text = "\n".join(p.get_text(separator=" ").strip() for p in paragraphs)
    return text


def summarise(text: str, max_length: int = 150, min_length: int = 40) -> str:
    """
    Produce a short summary of the input text.  Attempts to use a
    transformer model if available; otherwise returns the first few
    sentences as a crude summary.

    Parameters
    ----------
    text : str
        Full article text to summarise.
    max_length : int
        The maximum number of tokens (not words) in the summary when using
        the transformer pipeline.
    min_length : int
        The minimum number of tokens in the summary.

    Returns
    -------
    str
        A summary of the input text.
    """
    # Fallback for extremely short texts
    if len(text.split()) < 50:
        return text
    if pipeline is not None:
        # Lazy initialisation of summariser; models can be large
        global _summariser
        try:
            _summariser
        except NameError:
            _summariser = pipeline("summarization", model="t5-small", tokenizer="t5-small")
        summary_list = _summariser(
            text,
            max_length=max_length,
            min_length=min_length,
            do_sample=False,
        )
        return summary_list[0]["summary_text"]
    else:
        # Simple heuristic: return first two paragraphs
        sentences = text.split(". ")
        return ". ".join(sentences[:2]) + ("..." if len(sentences) > 2 else "")


def build_newsletter_html(articles: List[Article]) -> str:
    """
    Build an HTML string for the newsletter from a list of articles.

    Parameters
    ----------
    articles : List[Article]
        A list of Article dataclass instances.

    Returns
    -------
    str
        An HTML string ready to be sent via e‑mail.
    """
    html_parts = [
        "<html><head><meta charset='utf-8'><style>body{font-family:Arial,Helvetica,sans-serif;} h2{color:#2a2f45;} .article{margin-bottom:20px;} .source{font-size:12px;color:#777;} .summary{margin-top:5px;}</style></head><body>",
        "<h1>Resumo diário de notícias</h1>",
    ]
    for article in articles:
        html_parts.append("<div class='article'>")
        html_parts.append(f"<div class='source'><strong>{article.source}</strong></div>")
        html_parts.append(f"<h2><a href='{article.url}' target='_blank'>{article.title}</a></h2>")
        html_parts.append(f"<div class='summary'>{article.summary}</div>")
        html_parts.append("</div>")
    html_parts.append("</body></html>")
    return "\n".join(html_parts)


def send_email(subject: str, html_body: str, email_user: str, email_pass: str, dest_email: str) -> None:
    """
    Send an HTML e‑mail using SMTP over SSL (for example Gmail).  Adjust
    the SMTP server and port if using another provider.

    Parameters
    ----------
    subject : str
        The subject line of the e‑mail.
    html_body : str
        The HTML content to send.
    email_user : str
        Sender's e‑mail address.
    email_pass : str
        Password or app password for the e‑mail account.
    dest_email : str
        Recipient's e‑mail address.
    """
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = email_user
    msg["To"] = dest_email
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    # Example uses Gmail SMTP settings; change host/port as needed
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(email_user, email_pass)
        server.sendmail(email_user, [dest_email], msg.as_string())


def collect_articles(driver: webdriver.Chrome, creds: dict) -> List[Article]:
    """
    High level orchestrator for logging into each newspaper and collecting
    articles from the specified sections.  Adjust selectors and URLs
    according to the current layout of each site.

    Parameters
    ----------
    driver : webdriver.Chrome
        The Selenium WebDriver instance.
    creds : dict
        A dictionary containing login credentials (keys defined in
        ``load_credentials``).

    Returns
    -------
    List[Article]
        A list of Article objects with summaries.
    """
    articles: List[Article] = []

    # --------------- Estadão ---------------
    # Example: login selectors might change; inspect the page and update
    estadao_login_url = "https://login.estadao.com.br/"  # adjust if necessary
    estadao_selectors = {
        "username": "input[name='email']",
        "password": "input[name='password']",
        "submit": "button[type='submit']",
    }
    login_with_credentials(
        driver,
        estadao_login_url,
        creds["estadao_user"],
        creds["estadao_pass"],
        estadao_selectors,
    )
    # Collect politics and economics sections
    estadao_sections = [
        ("Estadão - Política", "https://politica.estadao.com.br"),
        ("Estadão - Economia", "https://economia.estadao.com.br"),
    ]
    for source_name, url in estadao_sections:
        links = extract_links(driver, url, "article a")  # adjust selector
        for title, link in links[:5]:  # limit number per section
            text = extract_article_text(driver, link)
            summary = summarise(text)
            articles.append(Article(source=source_name, title=title, url=link, summary=summary))

    # --------------- Valor Econômico ---------------
    valor_login_url = "https://login.valor.globo.com"  # placeholder
    valor_selectors = {
        "username": "input[type='email']",
        "password": "input[type='password']",
        "submit": "button[type='submit']",
    }
    login_with_credentials(
        driver,
        valor_login_url,
        creds["valor_user"],
        creds["valor_pass"],
        valor_selectors,
    )
    valor_sections = [
        ("Valor - Primeiro Caderno", "https://valor.globo.com/primeiro-caderno"),
        ("Valor - Empresas", "https://valor.globo.com/empresas"),
        ("Valor - Finanças", "https://valor.globo.com/financas"),
    ]
    for source_name, url in valor_sections:
        links = extract_links(driver, url, "article a")
        for title, link in links[:5]:
            text = extract_article_text(driver, link)
            summary = summarise(text)
            articles.append(Article(source=source_name, title=title, url=link, summary=summary))

    # --------------- O Globo ---------------
    oglobo_login_url = "https://login.globo.com"  # placeholder
    oglobo_selectors = {
        "username": "input[type='email']",
        "password": "input[type='password']",
        "submit": "button[type='submit']",
    }
    login_with_credentials(
        driver,
        oglobo_login_url,
        creds["oglobo_user"],
        creds["oglobo_pass"],
        oglobo_selectors,
    )
    oglobo_sections = [
        ("O Globo - Primeiro Caderno", "https://oglobo.globo.com/brasil"),
    ]
    for source_name, url in oglobo_sections:
        links = extract_links(driver, url, "article a")
        for title, link in links[:5]:
            text = extract_article_text(driver, link)
            summary = summarise(text)
            articles.append(Article(source=source_name, title=title, url=link, summary=summary))

    # --------------- The New York Times ---------------
    nyt_login_url = "https://myaccount.nytimes.com/auth/login"  # placeholder
    nyt_selectors = {
        "username": "input[id='email']",
        "password": "input[id='password']",
        "submit": "button[type='submit']",
    }
    login_with_credentials(
        driver,
        nyt_login_url,
        creds["nyt_user"],
        creds["nyt_pass"],
        nyt_selectors,
    )
    nyt_sections = [
        ("NYT - Geral", "https://www.nytimes.com/section/world"),
        ("NYT - Business", "https://www.nytimes.com/section/business"),
        ("NYT - Finance", "https://www.nytimes.com/section/business/dealbook"),
        ("NYT - Opinion", "https://www.nytimes.com/section/opinion"),
    ]
    for source_name, url in nyt_sections:
        links = extract_links(driver, url, "article a")
        for title, link in links[:5]:
            text = extract_article_text(driver, link)
            summary = summarise(text)
            articles.append(Article(source=source_name, title=title, url=link, summary=summary))

    return articles


def run_newsletter() -> None:
    """
    Main execution function.  Loads credentials, initiates the WebDriver,
    collects articles from all configured newspapers, builds the
    newsletter, and sends it via e‑mail.
    """
    creds = load_credentials()
    driver = init_driver()
    try:
        articles = collect_articles(driver, creds)
        if not articles:
            raise RuntimeError("Nenhum artigo foi coletado. Verifique as configurações e seletores.")
        html_body = build_newsletter_html(articles)
        subject = f"Resumo diário de notícias – {time.strftime('%d/%m/%Y')}"
        send_email(subject, html_body, creds["email_user"], creds["email_pass"], creds["dest_email"])
        print("Newsletter enviada com sucesso!")
    finally:
        driver.quit()


if __name__ == "__main__":
    # If you wish to schedule the newsletter within the script, uncomment
    # the block below and install the ``schedule`` library.  It will run
    # ``run_newsletter`` every day at 07:00 (São Paulo time).  You can
    # otherwise call ``run_newsletter`` directly or use an external
    # scheduler (cron/Task Scheduler/GitHub Actions).

    # import schedule
    # schedule.every().day.at("07:00").do(run_newsletter)
    # while True:
    #     schedule.run_pending()
    #     time.sleep(60)

    # For manual execution, simply call the function:
    run_newsletter()