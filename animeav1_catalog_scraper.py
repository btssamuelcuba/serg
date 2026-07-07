"""
AnimeAV1 Catalog Scraper — Scrapea el directorio completo por letras (A-Z + 0)
de animeav1.com/catalogo?letter=X y sube las fichas a WordPress vía REST API.

Uso local:
    # Una sola letra:
    python animeav1_catalog_scraper.py --letters A

    # Varias letras:
    python animeav1_catalog_scraper.py --letters A B C

    # Números (animes que empiezan por dígito):
    python animeav1_catalog_scraper.py --letters 0

    # Todo el catálogo:
    # Todo el catálogo:
    python animeav1_catalog_scraper.py --letters ALL

    # Solo la página 1 de la letra B:
    python animeav1_catalog_scraper.py --letters B --pages 1

    # Páginas 1 a 7 de la letra B:
    python animeav1_catalog_scraper.py --letters B --pages 1-7

    # Rango aplicado a varias letras (mismo rango para todas):
    python animeav1_catalog_scraper.py --letters A B C --pages 3-5

    # Slug directo:
    python animeav1_catalog_scraper.py --slugs shingeki-no-kyojin

    # Varios slugs:
    python animeav1_catalog_scraper.py --slugs shingeki-no-kyojin one-piece naruto

Variables de entorno (o editar CONFIG abajo):
    WP_URL            URL base de WordPress (sin barra final)
    WP_USER           Usuario administrador de WordPress
    WP_APP_PASSWORD   Application Password de WordPress
"""

import asyncio
import json
import os
import base64
import sys
import argparse
from datetime import datetime, timezone

import requests
from playwright.async_api import async_playwright

# ─────────────────────────────────────────────────────────────
#  CONFIG  — editar aquí o usar variables de entorno
# ─────────────────────────────────────────────────────────────
ANIMEAV1_BASE   = "https://animeav1.com"
CATALOG_URL     = "https://animeav1.com/catalogo"
HEADLESS        = True

# ── GitHub commit ─────────────────────────────────────────────
GITHUB_TOKEN     = os.getenv("G_TOKEN", "")
GITHUB_REPO      = os.getenv("G_REPO", "")
GITHUB_BRANCH    = os.getenv("G_BRANCH", "main")
GITHUB_JSON_DIR  = os.getenv("G_JSON_DIR", "catalog")  # carpeta dentro del repo

# ── Checkpoints ───────────────────────────────────────────────
CHECKPOINT_FILE       = "catalog_checkpoint.json"
FICHA_CHECKPOINT_FILE = "ficha_checkpoint.json"

# ── Letras disponibles ────────────────────────────────────────
ALL_LETTERS = ["0"] + list("ABCDEFGHIJKLMNOPQRSTUVWXYZ")

# ── Sin límite de episodios — scrapea todos los episodios completos ──
MAX_EPISODES_PER_RUN = float("inf")
SERVER_TIMEOUT       = 2   # segundos máx esperando cambio de iframe
# ─────────────────────────────────────────────────────────────

def parse_page_range(value: str):
    """
    Convierte "7" -> (7, 7)
    Convierte "1-7" -> (1, 7)
    Devuelve None si value es None/vacío (significa: todas las páginas).
    """
    if not value:
        return None

    value = value.strip()
    if "-" in value:
        start_str, end_str = value.split("-", 1)
        start, end = int(start_str), int(end_str)
    else:
        start = end = int(value)

    if start < 1 or end < 1 or start > end:
        raise ValueError(f"Rango de páginas inválido: '{value}'")

    return (start, end)

def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


# ─────────────────────────────────────────────────────────────
#  CHECKPOINTS
# ─────────────────────────────────────────────────────────────

def load_catalog_checkpoint() -> dict:
    if os.path.exists(CHECKPOINT_FILE):
        try:
            with open(CHECKPOINT_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_catalog_checkpoint(data: dict):
    with open(CHECKPOINT_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)



# Ruta absoluta basada en la carpeta del script, NO en el cwd desde donde se lanza.
SCRIPT_DIR             = os.path.dirname(os.path.abspath(__file__))
FICHA_CHECKPOINT_PATH  = os.path.join(SCRIPT_DIR, FICHA_CHECKPOINT_FILE)


def load_ficha_checkpoint() -> dict:
    log(f"  🔍 Buscando checkpoint en: {FICHA_CHECKPOINT_PATH}")
    if os.path.exists(FICHA_CHECKPOINT_PATH):
        try:
            with open(FICHA_CHECKPOINT_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            log(f"  ✅ Checkpoint cargado — slugs guardados: {list(data.keys())}")
            return data
        except Exception as e:
            log(f"  ❌ ERROR cargando ficha_checkpoint.json (puede estar corrupto): {e}")
            # Guardamos el archivo roto aparte para poder inspeccionarlo después,
            # en vez de pisarlo silenciosamente en el próximo save.
            try:
                os.replace(FICHA_CHECKPOINT_PATH, FICHA_CHECKPOINT_PATH + ".corrupto")
                log(f"  ⚠ Se renombró a {FICHA_CHECKPOINT_FILE}.corrupto para inspección")
            except Exception:
                pass
    else:
        log(f"  ⚠ No existe ficha_checkpoint.json en esa ruta — arranca vacío")
    return {}


def save_ficha_checkpoint(data: dict):
    # Escritura atómica: primero a un .tmp y luego rename, para que un Ctrl+C
    # a mitad de la escritura nunca deje el JSON corrupto.
    tmp_path = FICHA_CHECKPOINT_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, FICHA_CHECKPOINT_PATH)

# ─────────────────────────────────────────────────────────────
#  SCRAPING DEL CATÁLOGO
# ─────────────────────────────────────────────────────────────

async def get_total_pages(page, letter: str) -> int:
    url = f"{CATALOG_URL}?letter={letter}"
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_selector("main", timeout=15_000)
    except Exception as e:
        log(f"  ⚠ Error cargando {url}: {e}")
        return 1

    max_page = 1

    try:
        page_links = await page.locator("a[href*='letter='][href*='page=']").all()
        for link in page_links:
            href = await link.get_attribute("href") or ""
            if "page=" in href:
                try:
                    page_num = int(href.split("page=")[-1].split("&")[0])
                    if page_num > max_page:
                        max_page = page_num
                except ValueError:
                    pass
    except Exception:
        pass

    try:
        spans = await page.locator("div.flex.flex-wrap.gap-2 span").all()
        for span in spans:
            text = (await span.inner_text()).strip()
            try:
                num = int(text)
                if num > max_page:
                    max_page = num
            except ValueError:
                pass
    except Exception:
        pass

    return max_page


async def scrape_catalog_page(page, letter: str, page_num: int) -> list:
    if page_num == 1:
        url = f"{CATALOG_URL}?letter={letter}"
    else:
        url = f"{CATALOG_URL}?letter={letter}&page={page_num}"

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_selector("main article", timeout=15_000)
    except Exception as e:
        log(f"  ⚠ Error cargando página {page_num} de letra '{letter}': {e}")
        return []

    articles = await page.locator("main article").all()
    animes = []

    for article in articles:
        try:
            title_el = article.locator("h3").first
            title = (await title_el.inner_text()).strip()

            link_el = article.locator("a[href*='/media/']").first
            href = await link_el.get_attribute("href") or ""
            if not href:
                continue

            slug = href.rstrip("/").split("/media/")[-1]
            if not slug:
                continue

            img_el = article.locator("img.aspect-poster").first
            cover_url = await img_el.get_attribute("src") or ""

            type_el = article.locator("div.rounded.bg-line").first
            anime_type = ""
            try:
                anime_type = (await type_el.inner_text()).strip()
            except Exception:
                pass

            animes.append({
                "title": title,
                "slug": slug,
                "cover_url": cover_url,
                "type": anime_type,
                "source_url": f"{ANIMEAV1_BASE}{href}",
            })

        except Exception as e:
            log(f"    ⚠ Error leyendo artículo: {e}")
            continue

    return animes


async def scrape_letter(page, letter: str, page_range: tuple = None) -> list:
    log(f"\n{'═' * 50}")
    log(f"📂 Procesando letra: {letter}")

    total_pages = await get_total_pages(page, letter)
    log(f"  📄 Páginas detectadas: {total_pages}")

    # ── Resolver qué páginas tocan según page_range ──
    if page_range:
        start, end = page_range
        end = min(end, total_pages)  # no pasarse del total real
        if start > total_pages:
            log(f"  ⚠ La letra '{letter}' solo tiene {total_pages} páginas — "
                f"el rango solicitado ({start}-{page_range[1]}) no aplica, se omite.")
            return []
        pages_to_scrape = range(start, end + 1)
        log(f"  🎯 Rango solicitado: página(s) {start}-{end}")
    else:
        pages_to_scrape = range(1, total_pages + 1)

    all_animes = []

    for page_num in pages_to_scrape:
        log(f"  → Página {page_num}/{total_pages}...")
        animes = await scrape_catalog_page(page, letter, page_num)
        log(f"    ✓ {len(animes)} animes encontrados")
        all_animes.extend(animes)
        await asyncio.sleep(0.5)

    seen = set()
    unique = []
    for a in all_animes:
        if a["slug"] not in seen:
            seen.add(a["slug"])
            unique.append(a)

    log(f"  ✅ Total únicos en letra '{letter}': {len(unique)}")
    return unique


# ─────────────────────────────────────────────────────────────
#  SCRAPING DE FICHAS
# ─────────────────────────────────────────────────────────────


async def scrape_episode_downloads(page):

    downloads = []

    try:

        # botón descargar
        dl_btn = page.locator('button[aria-label="Descargar"]')

        await dl_btn.wait_for(
            state="visible",
            timeout=8000
        )

        await dl_btn.click(force=True)

        # esperar a que el modal exista
        await page.wait_for_selector(
            '[data-dialog-content][data-state="open"]',
            timeout=8000
        )

    except Exception as e:
        log(f"      ⚠ No se pudo abrir modal de descargas: {e}")
        return downloads


    try:

        links = page.locator(
            '[data-dialog-content][data-state="open"] a[href]'
        )

        total = await links.count()

        log(f"      📥 {total} enlaces encontrados")

        for i in range(total):

            a = links.nth(i)

            href = await a.get_attribute("href") or ""

            if not href:
                continue

            try:
                name = (
                    await a.locator("span.truncate").inner_text()
                ).strip()
            except:
                name = ""

            try:
                tipo = (
                    await a.locator("span.btn.btn-xs").first.inner_text()
                ).strip().upper()
            except:
                tipo = "SUB"

            downloads.append({
                "name": name,
                "type": tipo,
                "url": href
            })

            log(f"      ✓ [{tipo}] {name}")

    except Exception as e:

        log(f"      ⚠ Error leyendo descargas: {e}")


    # cerrar modal
    try:

        close_btn = page.locator("[data-dialog-close]")

        if await close_btn.count():

            await close_btn.first.click()

            await page.wait_for_timeout(300)

    except:

        pass

    return downloads

async def get_iframe_src(page) -> str:
    try:
        iframe = page.locator("iframe.aspect-video")
        return await iframe.get_attribute("src", timeout=2000) or ""
    except Exception:
        return ""


async def scrape_episode_servers(page, url: str) -> dict:
    """
    Visita la página del episodio, hace clic en cada botón de servidor
    (SUB y DUB) y captura el src del iframe.
    """
    servers = {"SUB": [], "DUB": []}

    log(f"      🌐 Cargando: ...{url[-60:]}")
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)

        # ── Cerrar cualquier modal que haya quedado abierto ────────
        try:
            await page.evaluate("""
                () => {
                    document.querySelectorAll('[data-state="open"]').forEach(
                        el => el.setAttribute('data-state', 'closed')
                    );
                }
            """)
            await page.wait_for_timeout(300)
        except Exception:
            pass

        await page.wait_for_selector("button.btn-xs", timeout=8_000)

    except Exception as e:
        log(f"      ⚠ Error cargando ep: {e}")
        return servers

    # ── Cerrar posible modal de login ──────────────────────────────
    try:
        login = page.locator('[data-dialog-overlay][data-state="open"]')
        if await login.count():
            log("      🔒 Modal de login detectado")
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(500)
    except Exception:
        pass

    # ── Descargas ──────────────────────────────────────────────────
    downloads = await scrape_episode_downloads(page)
    servers["downloads"] = downloads
    log(f"      📥 Descargas encontradas: {len(downloads)}")

    try:
        rows = await page.locator("div.flex.gap-3").all()
    except Exception:
        return servers

    for row in rows:
        try:
            first_span = row.locator("span").first
            span_class = await first_span.get_attribute("class", timeout=500) or ""
        except Exception:
            continue

        if "ic-sub" in span_class:
            audio_type = "SUB"
        elif "ic-dub" in span_class:
            audio_type = "DUB"
        else:
            continue

        try:
            buttons = await row.locator("button").all()
        except Exception:
            continue

        seen_urls = set()
        log(f"      🔍 [{audio_type}] {len(buttons)} servidor(es)…")

        for btn in buttons:
            try:
                btn_name = (await btn.inner_text()).strip()
            except Exception:
                continue

            prev_src = await get_iframe_src(page)

            try:
                await btn.click(timeout=3_000)
            except Exception:
                continue

            new_src = prev_src
            # Máximo SERVER_TIMEOUT segundos esperando cambio de iframe
            for _ in range(SERVER_TIMEOUT * 10):
                await asyncio.sleep(0.1)
                cur = await get_iframe_src(page)
                if cur and cur != prev_src:
                    new_src = cur
                    break

            if new_src and new_src not in seen_urls:
                seen_urls.add(new_src)
                servers[audio_type].append({"name": btn_name, "url": new_src})
                log(f"        ✓ {btn_name} → {new_src[:55]}…")

    return servers


async def scrape_anime_ficha(page, slug: str) -> dict:
    """
    Visita https://animeav1.com/media/{slug} y extrae todos los metadatos
    y la lista completa de episodios.
    """
    url = f"{ANIMEAV1_BASE}/media/{slug}"
    ficha = {
        "slug": slug,
        "title": "",
        "alt_title": "",
        "type": "",
        "year": "",
        "season": "",
        "status": "",
        "genres": [],
        "synopsis": "",
        "score": "0",
        "votes": "0",
        "cover_url": "",
        "backdrop_url": "",
        "episodes": [],
    }

    try:
        log(f"  📄 Scrapeando ficha: {url}")
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_selector("article", timeout=15_000)
    except Exception as e:
        log(f"  ⚠ Error cargando ficha: {e}")
        return ficha

    # ── Cover ──────────────────────────────────────────────────
    try:
        cover_el = page.locator("div.grid.w-full.shrink-0 img.aspect-poster").first
        ficha["cover_url"] = await cover_el.get_attribute("src") or ""
    except Exception:
        pass

    # ── Backdrop ───────────────────────────────────────────────
    try:
        backdrop_el = page.locator("figure.absolute img").first
        ficha["backdrop_url"] = await backdrop_el.get_attribute("src") or ""
    except Exception:
        pass

    # ── Título principal y alternativo ─────────────────────────
    try:
        h1 = page.locator("h1.text-lead").first
        ficha["title"] = (await h1.inner_text()).strip()
    except Exception:
        pass

    try:
        h2 = page.locator("h2.text-main").first
        ficha["alt_title"] = (await h2.inner_text()).strip()
    except Exception:
        pass

    # ── Tipo / Año / Temporada / Estado ────────────────────────
    try:
        meta_spans = await page.locator(
            "div.flex.flex-wrap.items-center.gap-2.text-sm span"
        ).all()
        meta_texts = []
        for sp in meta_spans:
            t = (await sp.inner_text()).strip()
            if t and t != "•":
                meta_texts.append(t)
        if len(meta_texts) >= 1:
            ficha["type"] = meta_texts[0]
        if len(meta_texts) >= 2:
            ficha["year"] = meta_texts[1]
        if len(meta_texts) >= 3:
            ficha["season"] = meta_texts[2]
        if len(meta_texts) >= 4:
            ficha["status"] = meta_texts[3]
    except Exception:
        pass

    # ── Géneros ────────────────────────────────────────────────
    try:
        genre_links = await page.locator(
            "div.flex.flex-wrap.items-center.gap-2 a[href*='/catalogo?genre=']"
        ).all()
        for a in genre_links:
            g = (await a.inner_text()).strip()
            if g:
                ficha["genres"].append(g)
    except Exception:
        pass

    # ── Sinopsis ───────────────────────────────────────────────
    try:
        synopsis_el = page.locator("div.entry p").first
        ficha["synopsis"] = (await synopsis_el.inner_text()).strip()
    except Exception:
        pass

    # ── Puntuación y votos ─────────────────────────────────────
    try:
        score_el = page.locator("div.text-lead.text-2xl.font-bold").first
        ficha["score"] = (await score_el.inner_text()).strip()
    except Exception:
        pass

    try:
        votes_el = page.locator("span.font-bold").first
        ficha["votes"] = (await votes_el.inner_text()).strip()
    except Exception:
        pass

    # ── Episodios ──────────────────────────────────────────────
    try:
        await page.wait_for_selector("section article", timeout=10_000)
    except Exception:
        log("  ⚠ No se cargaron artículos de episodios")
        return ficha

    episodes = []

    # Selector de items del dropdown de rangos
    ITEMS_SELECTOR = "[data-dropdown-menu-content] [data-dropdown-menu-item]"
    trigger_loc    = page.locator("[data-dropdown-menu-trigger]").first
    range_count    = 0

    # Intentar abrir el dropdown para contar cuántos rangos hay
    try:
        t_count = await page.locator("[data-dropdown-menu-trigger]").count()
        if t_count > 0:
            await trigger_loc.click(timeout=5_000)
            # Esperar a que los items existan de verdad, no un sleep fijo
            await page.wait_for_selector(ITEMS_SELECTOR, timeout=5_000, state="visible")
            range_count = await page.locator(ITEMS_SELECTOR).count()
    except Exception:
        range_count = 0

    if range_count > 0:
        log(f"  📂 Anime con rangos: {range_count} bloques de episodios")
        for rb_idx in range(range_count):
            rb_ok = False
            for attempt in range(1, 4):  # hasta 3 intentos por rango
                try:
                    # 1. Reabrir el dropdown antes de cada click (se cierra solo al seleccionar)
                    await trigger_loc.click(timeout=5_000)
                    await page.wait_for_selector(ITEMS_SELECTOR, timeout=5_000, state="visible")

                    # 2. Leer el texto del item ANTES de hacer clic (para el log)
                    item = page.locator(ITEMS_SELECTOR).nth(rb_idx)
                    rb_text = (await item.inner_text(timeout=5_000)).strip()
                    suffix = f" (intento {attempt})" if attempt > 1 else ""
                    log(f"  → Rango {rb_idx + 1}/{range_count}: {rb_text}{suffix}")

                    # 3. Capturar cuántos artículos hay ANTES del clic
                    prev_count = await page.locator("section article").count()

                    # 4. Hacer clic en el item del rango
                    await item.click(timeout=5_000)

                    # 5. Esperar a que el contenido CAMBIE (no solo que exista)
                    try:
                        await page.wait_for_function(
                            """() => {
                                const arts = document.querySelectorAll('section article');
                                if (arts.length === 0) return false;
                                const firstNum = arts[0].querySelector('span.text-lead.font-bold');
                                return firstNum !== null;
                            }""",
                            timeout=8_000,
                        )
                    except Exception:
                        pass

                    # 6. Pausa corta extra para renders lentos
                    await page.wait_for_timeout(600)

                    ep_articles = await page.locator("section article").all()
                    log(f"    {len(ep_articles)} episodios en este rango")

                    if len(ep_articles) == 0:
                        raise RuntimeError("0 episodios — probablemente cargó antes de tiempo")

                    # 7. Verificar contenido stale (misma cantidad y mismo primer episodio)
                    if rb_idx > 0 and prev_count == len(ep_articles):
                        try:
                            first_num_el = ep_articles[0].locator("span.text-lead.font-bold").first
                            first_num = (await first_num_el.inner_text()).strip()
                            already_seen = any(e["num"] == first_num for e in episodes)
                            if already_seen:
                                raise RuntimeError(f"Contenido stale detectado (ep {first_num} ya existe) — reintentando")
                        except RuntimeError:
                            raise
                        except Exception:
                            pass

                    for art in ep_articles:
                        try:
                            num_el = art.locator("span.text-lead.font-bold").first
                            num = (await num_el.inner_text()).strip()
                            img_el = art.locator("img").first
                            thumb = await img_el.get_attribute("src") or ""
                            link_el = art.locator("a[href]").first
                            href = await link_el.get_attribute("href") or ""
                            episodes.append({"num": num, "thumb_url": thumb, "href": href})
                        except Exception:
                            continue

                    rb_ok = True
                    break  # éxito — no reintentar este rango
                except Exception as e:
                    log(f"  ⚠ Error en rango {rb_idx + 1} (intento {attempt}/3): {e}")
                    await page.wait_for_timeout(1_200)  # más pausa entre reintentos
                    continue

            if not rb_ok:
                log(f"  ❌ Rango {rb_idx + 1} falló tras 3 intentos — esos episodios se pierden esta corrida")
    else:
        log(f"  📄 Sin rangos — cargando episodios directos…")
        try:
            ep_articles = await page.locator("section article").all()
            log(f"    {len(ep_articles)} episodios encontrados")
            for art in ep_articles:
                try:
                    num_el = art.locator("span.text-lead.font-bold").first
                    num = (await num_el.inner_text()).strip()
                    img_el = art.locator("img").first
                    thumb = await img_el.get_attribute("src") or ""
                    link_el = art.locator("a[href]").first
                    href = await link_el.get_attribute("href") or ""
                    episodes.append({"num": num, "thumb_url": thumb, "href": href})
                except Exception:
                    continue
        except Exception:
            pass

    # Deduplicar y ordenar
    seen_nums = set()
    unique_eps = []
    for ep in episodes:
        if ep["num"] not in seen_nums:
            seen_nums.add(ep["num"])
            unique_eps.append(ep)

    ficha["episodes"] = sorted(
        unique_eps,
        key=lambda x: float(x["num"]) if x["num"].replace(".", "").isdigit() else 0
    )
    log(f"  ✓ Ficha: '{ficha['title']}' — {len(ficha['episodes'])} episodios")
    return ficha


async def scrape_episodes_servers_for_ficha(page, ficha: dict, ficha_checkpoint: dict) -> list:
    """
    Para cada episodio de la ficha, scrapea sus servidores con checkpoint.
    """
    slug    = ficha["slug"]
    all_eps = ficha["episodes"]
    total   = len(all_eps)

    done_nums = set(ficha_checkpoint.get(slug, {}).get("done_episodes", []))
    pending   = [ep for ep in all_eps if ep["num"] not in done_nums]

    log(f"  📺 {total} episodios totales, {len(pending)} pendientes")

    result           = list(ficha_checkpoint.get(slug, {}).get("episodes_data", []))
    processed_this_run = 0

for ep in pending:

        num  = ep["num"]
        href = ep.get("href", "")
        if href and not href.startswith("http"):
            href = f"{ANIMEAV1_BASE}{href}"
        if not href:
            href = f"{ANIMEAV1_BASE}/media/{slug}/{num}"

        log(f"\n    ── Ep. {num} ({processed_this_run + 1}/{total}) ──")
        log(f"    🔗 {href[:80]}")
        servers   = await scrape_episode_servers(page, href)
        sub_count = len(servers.get("SUB", []))
        dub_count = len(servers.get("DUB", []))
        dl_count = len(servers.get("downloads", []))
        log(f"    ✅ Ep. {num} — {sub_count} SUB, {dub_count} DUB, {dl_count} descargas")

        ep_data = {
            "slug":       slug,
            "title":      ficha["title"],
            "episode":    num,
            "thumbnail":  ep.get("thumb_url", ""),
            "source_url": href,
            "local_url":  f"/ver/{slug}/{num}",
            "time_ago":   "",
            "servers":    servers,
            "scraped_at": datetime.now(timezone.utc).isoformat(),
        }
        result.append(ep_data)
        done_nums.add(num)
        processed_this_run += 1

        ficha_checkpoint[slug] = {
            "title":         ficha.get("title", slug),
            "done_episodes": list(done_nums),
            "episodes_data": result,
            "total":         total,
            "completed":     len(done_nums) >= total,
        }
        save_ficha_checkpoint(ficha_checkpoint)

    remaining = [ep for ep in all_eps if ep["num"] not in done_nums]
    if not remaining:
        # Asegurarse de que la clave existe antes de escribir en ella
        if slug not in ficha_checkpoint:
            ficha_checkpoint[slug] = {
                "title":         ficha.get("title", slug),
                "done_episodes": list(done_nums),
                "episodes_data": result,
                "total":         total,
                "completed":     True,
            }
        else:
            ficha_checkpoint[slug]["completed"] = True
        save_ficha_checkpoint(ficha_checkpoint)
        log(f"  ✅ Ficha '{ficha['title']}' completada")
    else:
        log(f"  ⏳ Quedan {len(remaining)} eps — se reanudarán en el próximo run")

    return result


# ─────────────────────────────────────────────────────────────
#  GUARDAR / COMMITEAR JSON POR LETRA
# ─────────────────────────────────────────────────────────────

def save_letter_json(letter: str, animes: list):
    # Asegura que la carpeta catalog exista localmente
    os.makedirs("catalog", exist_ok=True)
    
    filename = f"catalog/{letter.upper()}.json"
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(animes, f, ensure_ascii=False, indent=2)
    log(f"  💾 JSON guardado en carpeta catalog: {filename} ({len(animes)} animes)")


def commit_letter_json_to_github(letter: str, animes: list) -> bool:
    # Ya no necesitamos hacer nada aquí porque Git se encargará desde el workflow.
    # Retornamos True para que el script continúe sin errores.
    return True

    filename = f"{letter.upper()}.json"
    gh_path  = f"{GITHUB_JSON_DIR}/{filename}" if GITHUB_JSON_DIR else filename
    api_url  = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{gh_path}"
    headers  = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    sha = None
    try:
        r = requests.get(api_url, headers=headers, params={"ref": GITHUB_BRANCH}, timeout=10)
        if r.status_code == 200:
            sha = r.json().get("sha")
    except Exception as e:
        log(f"  ⚠ Error consultando GitHub: {e}")

    content_str = json.dumps(animes, ensure_ascii=False, indent=2)
    content_b64 = base64.b64encode(content_str.encode("utf-8")).decode("utf-8")

    payload = {
        "message": f"catalog: update {filename} ({len(animes)} animes) [{datetime.now().strftime('%Y-%m-%d %H:%M')}]",
        "content": content_b64,
        "branch":  GITHUB_BRANCH,
    }
    if sha:
        payload["sha"] = sha

    try:
        r = requests.put(api_url, headers=headers, json=payload, timeout=30)
        if r.status_code in (200, 201):
            log(f"  ✅ Commiteado en GitHub: {GITHUB_REPO}/{gh_path}")
            return True
        else:
            log(f"  ❌ Error GitHub API {r.status_code}: {r.text[:200]}")
            return False
    except Exception as e:
        log(f"  Excepción al commitear: {e}")
        return False


def save_slug_json(slug: str, data: list):
    """Guarda el JSON de un slug individual localmente (siempre como array)."""
    os.makedirs("slugs", exist_ok=True)
    filename = f"slugs/{slug}.json"
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    log(f"  💾 JSON guardado localmente: {filename}")


def commit_slug_json_to_github(slug: str, data: list) -> bool:
    """Commitea el JSON de un slug individual al repo de GitHub (siempre como array)."""
    if not GITHUB_TOKEN or not GITHUB_REPO:
        log("  ❌ G_TOKEN o G_REPO no configurados.")
        return False

    filename = f"{slug}.json"
    subdir   = f"{GITHUB_JSON_DIR}/slugs" if GITHUB_JSON_DIR else "slugs"
    gh_path  = f"{subdir}/{filename}"
    api_url  = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{gh_path}"
    headers  = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    sha = None
    try:
        r = requests.get(api_url, headers=headers, params={"ref": GITHUB_BRANCH}, timeout=10)
        if r.status_code == 200:
            sha = r.json().get("sha")
    except Exception as e:
        log(f"  ⚠ Error consultando GitHub: {e}")

    content_str = json.dumps(data, ensure_ascii=False, indent=2)
    content_b64 = base64.b64encode(content_str.encode("utf-8")).decode("utf-8")

    payload = {
        "message": f"catalog: slug {slug} [{datetime.now().strftime('%Y-%m-%d %H:%M')}]",
        "content": content_b64,
        "branch":  GITHUB_BRANCH,
    }
    if sha:
        payload["sha"] = sha

    try:
        r = requests.put(api_url, headers=headers, json=payload, timeout=30)
        if r.status_code in (200, 201):
            log(f"  ✅ Commiteado en GitHub: {GITHUB_REPO}/{gh_path}")
            return True
        else:
            log(f"  ❌ Error GitHub API {r.status_code}: {r.text[:200]}")
            return False
    except Exception as e:
        log(f"  Excepción al commitear: {e}")
        return False


# ─────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(
        description="AnimeAV1 Catalog Scraper — Scrapea el directorio por letras o slugs directos"
    )
    parser.add_argument(
        "--letters",
        nargs="+",
        default=[],
        help=(
            "Letras a procesar. Ejemplos: A  |  A B C  |  0  |  ALL\n"
            "'ALL' procesa todo el catálogo (0-9 + A-Z)"
        ),
)
    parser.add_argument(
        "--slugs",
        nargs="+",
        default=[],
        help=(
            "Slugs directos de animes a scrapear. Ejemplos:\n"
            "  --slugs shingeki-no-kyojin\n"
            "  --slugs shingeki-no-kyojin one-piece naruto\n"
            "Crea slugs/{slug}.json para cada uno."
        ),
    )
    parser.add_argument(
        "--pages",
        default=None,
        help=(
            "Página o rango de páginas a scrapear dentro de las letras indicadas.\n"
            "Solo aplica al modo --letters (se ignora en --slugs).\n"
            "Ejemplos:\n"
            "  --pages 1        (solo la página 1)\n"
            "  --pages 1-7      (de la página 1 a la 7)\n"
            "Si no se especifica, se scrapean TODAS las páginas de cada letra."
        ),
    )
    return parser.parse_args()


async def main():
    args = parse_args()

    if not args.letters and not args.slugs:
        print("❌ Debes pasar --letters o --slugs. Ejemplo: --letters A  |  --slugs shingeki-no-kyojin")
        sys.exit(1)

    # Resolver letras a procesar
    if args.letters:
        if "ALL" in [l.upper() for l in args.letters]:
            letters = ALL_LETTERS
        else:
            letters = [l.upper() for l in args.letters]
            for l in letters:
                if l not in ALL_LETTERS:
                    print(f"❌ Letra inválida: '{l}'. Válidas: 0 y A-Z")
                    sys.exit(1)
    else:
        letters = []

    slugs_direct = [s.strip().lower() for s in args.slugs] if args.slugs else []

    # Resolver rango de páginas (solo aplica a --letters)
    try:
        page_range = parse_page_range(args.pages)
    except ValueError as e:
        print(f"❌ {e}")
        sys.exit(1)

    log("═" * 60)
    log("AnimeAV1 Catalog Scraper")
    if letters:
        log(f"Letras    : {', '.join(letters)}")
    if page_range:
        log(f"Páginas   : {page_range[0]}-{page_range[1]}")
    if slugs_direct:
        log(f"Slugs     : {', '.join(slugs_direct)}")
    log(f"GitHub    : {GITHUB_REPO}/{GITHUB_JSON_DIR}")
    log("═" * 60)

    catalog_checkpoint = load_catalog_checkpoint()
    ficha_checkpoint   = load_ficha_checkpoint()

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=HEADLESS,
            args=["--no-sandbox", "--disable-setuid-sandbox"],
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )

        catalog_page = await context.new_page()
        ficha_page   = await context.new_page()

        # ── Modo letras (comportamiento original) ─────────────────
        for letter in letters:
            log(f"\n{'─' * 60}")
            log(f"▶ Letra: {letter}")

            animes_basic = await scrape_letter(catalog_page, letter, page_range)

            if not animes_basic:
                log(f"  ⚠ No se encontraron animes para '{letter}'")
                continue

            log(f"  📋 {len(animes_basic)} animes encontrados — scrapeando fichas…")

            animes_full = []
            for idx, anime in enumerate(animes_basic, 1):
                slug = anime["slug"]
                log(f"\n[{idx}/{len(animes_basic)}] {anime['title']} ({slug})")


                cached = catalog_checkpoint.get(letter, {}).get(slug)
                cached_eps = cached.get("episodes", []) if cached else []
                cached_has_servers = bool(cached_eps) and all("servers" in ep for ep in cached_eps)

                if cached and cached_has_servers:
                    log(f"  ✓ Ya scrapeado (con servidores) — cargando del checkpoint")
                    animes_full.append(cached)
                    continue    


                ficha = await scrape_anime_ficha(ficha_page, slug)
                if not ficha["title"]:
                    log(f"  ⚠ No se pudo obtener ficha — saltando")
                    continue

                # ── Scrapear servidores SUB/DUB de cada episodio ──
                episodes_with_servers = await scrape_episodes_servers_for_ficha(
                    ficha_page, ficha, ficha_checkpoint
                )
                ficha["episodes"] = episodes_with_servers

                if letter not in catalog_checkpoint:
                    catalog_checkpoint[letter] = {}
                catalog_checkpoint[letter][slug] = ficha
                save_catalog_checkpoint(catalog_checkpoint)

                animes_full.append(ficha)

            if animes_full:
                save_letter_json(letter, animes_full)
                commit_letter_json_to_github(letter, animes_full)

        # ── Modo slugs directos ───────────────────────────────────
        for idx, slug in enumerate(slugs_direct, 1):
            log(f"\n{'─' * 60}")
            log(f"▶ Slug directo [{idx}/{len(slugs_direct)}]: {slug}")

            ficha = await scrape_anime_ficha(ficha_page, slug)
            if not ficha["title"]:
                log(f"  ⚠ No se pudo obtener ficha para '{slug}' — saltando")
                continue

            # Scrapear servidores de cada episodio
            episodes_with_servers = await scrape_episodes_servers_for_ficha(
                ficha_page, ficha, ficha_checkpoint
            )

            # Reemplazar lista básica por la completa con servidores
            ficha["episodes"] = episodes_with_servers

            # Guardar siempre como array para compatibilidad con el importador
            save_slug_json(slug, [ficha])
            commit_slug_json_to_github(slug, [ficha])

        await catalog_page.close()
        await ficha_page.close()
        await browser.close()

    log("\n" + "═" * 60)
    log("✅ Catalog Scraper finalizado.")


if __name__ == "__main__":
    asyncio.run(main())