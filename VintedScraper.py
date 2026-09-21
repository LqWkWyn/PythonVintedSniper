import argparse
from html.parser import HTMLParser
import random
import os
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

from curl_cffi import requests

try:
    from vinted import Vinted
except ImportError:
    Vinted = None


class VintedCatalogParser(HTMLParser):
    def __init__(self, base_url):
        super().__init__()
        self.base_url = base_url
        self.items = []
        self._current_item = None

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return

        attributes = dict(attrs)
        href = attributes.get("href", "")
        parsed = urlparse(href)
        if parsed.path.startswith("/items/"):
            item_id = parsed.path.split("/", 2)[-1].split("-", 1)[0]
            if item_id.isdigit():
                self._current_item = {
                    "id": int(item_id),
                    "title": attributes.get("title", ""),
                    "url": urljoin(self.base_url, href),
                }

    def handle_data(self, data):
        if self._current_item is not None:
            self._current_item["title"] += data

    def handle_endtag(self, tag):
        if tag != "a" or self._current_item is None:
            return

        title = " ".join(self._current_item["title"].split())
        if title and not any(item["id"] == self._current_item["id"] for item in self.items):
            self._current_item["title"] = title
            self.items.append(self._current_item)
        self._current_item = None


class ContinuousVintedScraper:
    def __init__(self, domain="fr", proxy=None, proxies=None, use_wrapper=True,
                 discord_webhook_url=None, telegram_bot_token=None, telegram_chat_id=None,
                 log_callback=None):
        self.domain = self._normalize_domain(domain)
        self.base_url = f"https://www.vinted.{self.domain}"
        self.proxies = self._load_proxies(proxy=proxy, proxies=proxies)
        self.use_wrapper = use_wrapper and Vinted is not None
        self.discord_webhook_url = discord_webhook_url
        self.telegram_bot_token = telegram_bot_token
        self.telegram_chat_id = telegram_chat_id
        self.log_callback = log_callback
        self._client_proxy = None
        self._proxy_index = 0

        self.session = requests.Session(impersonate="chrome")
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": self.base_url,
        })
        self._apply_proxy_to_session()

        self.client = None
        if self.use_wrapper:
            self.client = self._build_wrapper_client()

        # Track seen item IDs to prevent duplicate alerts.
        self.seen_item_ids = set()
        self._init_session()

    def log(self, message):
        print(message)
        if self.log_callback:
            self.log_callback(message)

    @staticmethod
    def _normalize_domain(domain):
        domain = domain.replace("https://", "").replace("http://", "").strip().lower()
        if domain.startswith("www."):
            domain = domain[4:]
        if domain.startswith("vinted."):
            domain = domain.split(".", 1)[1]
        return domain

    @staticmethod
    def _load_proxies(proxy=None, proxies=None):
        proxy_list = []

        if proxy:
            proxy_list.append(proxy)

        if proxies:
            if isinstance(proxies, str):
                proxy_candidates = proxies.replace("\n", ",").split(",")
            else:
                proxy_candidates = proxies
            proxy_list.extend(proxy_candidates)

        env_single = os.getenv("VINTED_PROXY")
        if env_single:
            proxy_list.append(env_single)

        env_multi = os.getenv("VINTED_PROXIES")
        if env_multi:
            proxy_list.extend(env_multi.replace("\n", ",").split(","))

        cleaned = []
        for value in proxy_list:
            if value and value.strip():
                cleaned.append(value.strip())

        return cleaned

    def _current_proxy(self):
        if not self.proxies:
            return None
        return self.proxies[self._proxy_index % len(self.proxies)]

    def _advance_proxy(self):
        if self.proxies:
            self._proxy_index = (self._proxy_index + 1) % len(self.proxies)
            self._apply_proxy_to_session()
            if self.use_wrapper:
                self.client = self._build_wrapper_client()

    def _apply_proxy_to_session(self):
        proxy = self._current_proxy()
        if proxy:
            self.session.proxies.update({"http": proxy, "https": proxy})

    def _build_wrapper_client(self):
        proxy = self._current_proxy()
        self._client_proxy = proxy
        return Vinted(domain=self.domain, proxy=proxy)

    def _init_session(self):
        """Fetches fresh session cookies from the base website."""
        self.log(f"[*] [SESSION] Refreshing session cookies from {self.base_url}...")
        try:
            res = self.session.get(self.base_url, timeout=10)
            if res.status_code == 200:
                self.log("[+] [SESSION] Session initialized successfully.")
            else:
                self.log(f"[-] [SESSION] Failed to init session. HTTP {res.status_code}")
        except Exception as e:
            self.log(f"[!] [SESSION] Error fetching session cookies: {e}")

    @staticmethod
    def _get_value(item, key, default=None):
        if isinstance(item, dict):
            return item.get(key, default)
        return getattr(item, key, default)

    def _format_item(self, item):
        price = self._get_value(item, "price")
        currency = self._get_value(item, "currency", self._get_value(item, "currency_code", ""))

        if isinstance(price, dict):
            amount = price.get("amount")
            currency = price.get("currency_code", currency)
        else:
            amount = price

        return {
            "id": self._get_value(item, "id"),
            "title": self._get_value(item, "title", "Untitled item"),
            "price": amount,
            "currency": currency,
            "url": self._get_value(item, "url", ""),
            "brand": self._get_value(item, "brand_title", self._get_value(item, "brand", "")),
            "size": self._get_value(item, "size_title", self._get_value(item, "size", "")),
            "raw": item,
        }

    def fetch_latest(self, search_text="nike", search_url=None, per_page=20, page=1,
                     catalog_ids=None, brand_ids=None, price_from=None, price_to=None):
        """Queries Vinted for the newest matching items."""
        try:
            return self._fetch_latest_from_catalog_page(
                search_text=search_text,
                search_url=search_url,
                per_page=per_page,
                page=page,
                catalog_ids=catalog_ids,
                brand_ids=brand_ids,
                price_from=price_from,
                price_to=price_to,
            )

        except Exception as e:
            self.log(f"[!] [CATALOG] Network error during request: {e}")
            self._advance_proxy()
            return []

    def fetch_item_details(self, item_id):
        if self.client is None:
            return None

        try:
            if self._client_proxy != self._current_proxy():
                self.client = self._build_wrapper_client()
            response = self.client.item_info(item_id)
            return getattr(response, "item", None)
        except Exception as e:
            self.log(f"[!] [WRAPPER] Failed to fetch item details for {item_id}: {e}")
            self._advance_proxy()
            return None

    def _send_discord_notification(self, message):
        if not self.discord_webhook_url:
            return

        try:
            self.session.post(self.discord_webhook_url, json={"content": message}, timeout=10)
        except Exception as e:
            self.log(f"[!] [DISCORD] Failed to send notification: {e}")

    def _send_telegram_notification(self, message):
        if not self.telegram_bot_token or not self.telegram_chat_id:
            return

        url = f"https://api.telegram.org/bot{self.telegram_bot_token}/sendMessage"
        payload = {"chat_id": self.telegram_chat_id, "text": message, "disable_web_page_preview": False}
        try:
            self.session.post(url, json=payload, timeout=10)
        except Exception as e:
            self.log(f"[!] [TELEGRAM] Failed to send notification: {e}")

    def _notify_new_item(self, normalized_item, details=None):
        message_lines = [
            f"New Vinted listing: {normalized_item['title']}",
            f"Price: {normalized_item['price']} {normalized_item['currency']}",
        ]
        if normalized_item["brand"]:
            message_lines.append(f"Brand: {normalized_item['brand']}")
        if normalized_item["size"]:
            message_lines.append(f"Size: {normalized_item['size']}")
        if normalized_item["url"]:
            message_lines.append(f"Link: {normalized_item['url']}")

        if details is not None:
            user = self._get_value(details, "user")
            seller = self._get_value(user, "login", self._get_value(user, "name", ""))
            if seller:
                message_lines.append(f"Seller: {seller}")

        message = "\n".join(message_lines)
        self._send_discord_notification(message)
        self._send_telegram_notification(message)

    def _build_catalog_url(self, search_text, search_url, per_page, page,
                           catalog_ids, brand_ids, price_from, price_to):
        url = search_url or f"{self.base_url}/catalog"
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        params["search_text"] = [search_text]
        params["order"] = ["newest_first"]
        params["per_page"] = [str(per_page)]
        params["page"] = [str(page)]
        if catalog_ids:
            params["catalog_ids"] = [str(value) for value in catalog_ids]
        if brand_ids:
            params["brand_ids"] = [str(value) for value in brand_ids]
        if price_from is not None:
            params["price_from"] = [str(price_from)]
        if price_to is not None:
            params["price_to"] = [str(price_to)]
        return parsed._replace(query=urlencode(params, doseq=True)).geturl()

    def _fetch_latest_from_catalog_page(self, search_text, search_url, per_page, page,
                                        catalog_ids, brand_ids, price_from, price_to):
        catalog_url = self._build_catalog_url(
            search_text, search_url, per_page, page,
            catalog_ids, brand_ids, price_from, price_to,
        )
        response = self.session.get(catalog_url, timeout=10)
        if response.status_code in [401, 403]:
            self.log(f"[-] [CATALOG] Got status {response.status_code}. Session expired or flagged.")
            self._init_session()
            self._advance_proxy()
            return []
        if response.status_code == 429:
            self.log("[-] [CATALOG] Rate limited (429)! Cooldown triggered.")
            time.sleep(30)
            self._advance_proxy()
            return []
        if response.status_code != 200:
            self.log(f"[-] [CATALOG] Request failed with HTTP status {response.status_code}")
            return []

        parser = VintedCatalogParser(self.base_url)
        parser.feed(response.text)
        self.log(f"[+] [CATALOG] Parsed {len(parser.items)} listings from the catalog page.")
        return parser.items

    def start_monitoring(self, search_text="nike", min_delay=8, max_delay=18, search_url=None, per_page=20,
                         catalog_ids=None, brand_ids=None, price_from=None, price_to=None,
                         enrich_new_items=True, stop_event=None):
        """Runs the continuous monitoring loop."""
        self.log(f"[*] Starting continuous monitoring for: '{search_text}'")
        self.log(f"[*] Polling interval: Random between {min_delay}s and {max_delay}s\n")
        if self.client is not None:
            self.log(f"[*] [WRAPPER] Enabled for optional item details on domain '{self.domain}'.")
        else:
            self.log("[*] [WRAPPER] Disabled; using direct API only.")
        
        # First run: seed the seen_item_ids set so old listings aren't flagged as 'new'
        initial_items = self.fetch_latest(
            search_text=search_text,
            search_url=search_url,
            per_page=per_page,
            catalog_ids=catalog_ids,
            brand_ids=brand_ids,
            price_from=price_from,
            price_to=price_to,
        )
        for item in initial_items:
            self.seen_item_ids.add(self._get_value(item, "id"))
        self.log(f"[+] Initialized watchlist with {len(self.seen_item_ids)} existing items.\n")

        # Main Loop
        while not (stop_event and stop_event.is_set()):
            try:
                latest_items = self.fetch_latest(
                    search_text=search_text,
                    search_url=search_url,
                    per_page=per_page,
                    catalog_ids=catalog_ids,
                    brand_ids=brand_ids,
                    price_from=price_from,
                    price_to=price_to,
                )
                new_count = 0
                
                for item in latest_items:
                    item_id = self._get_value(item, "id")
                    
                    if item_id not in self.seen_item_ids:
                        self.seen_item_ids.add(item_id)
                        new_count += 1

                        normalized = self._format_item(item)
                        details = self.fetch_item_details(item_id) if enrich_new_items else None

                        title = normalized["title"]
                        price = normalized["price"]
                        currency = normalized["currency"]
                        url = normalized["url"]
                        brand = normalized["brand"]
                        size = normalized["size"]

                        self.log(f"🔥 [NEW LISTING FOUND] {title} | {price} {currency}")
                        if brand:
                            self.log(f"   Brand: {brand}")
                        if size:
                            self.log(f"   Size: {size}")
                        self.log(f"   Link: {url}")

                        if details is not None:
                            user = self._get_value(details, "user")
                            seller = self._get_value(user, "login", self._get_value(user, "name", ""))
                            if seller:
                                self.log(f"   Seller: {seller}")

                        self._notify_new_item(normalized, details)
                        
                        # --- DISCORD / TELEGRAM TRIGGER HERE ---

                if new_count == 0:
                    self.log(f"[{time.strftime('%H:%M:%S')}] Checked API. No new items.")

                # Keep memory clear if set gets too large over long runs
                if len(self.seen_item_ids) > 1000:
                    while len(self.seen_item_ids) > 500:
                        self.seen_item_ids.pop()

            except Exception as e:
                print(f"[!] Unexpected error in main loop: {e}")

            # Randomized Sleep Delay (Jitter)
            sleep_time = random.uniform(min_delay, max_delay)
            elapsed = 0
            while elapsed < sleep_time:
                if stop_event and stop_event.is_set():
                    break
                time.sleep(min(0.5, sleep_time - elapsed))
                elapsed += 0.5


def parse_csv_ints(value):
    if not value:
        return None
    if isinstance(value, list):
        return [int(item) for item in value if str(item).strip()]
    return [int(item.strip()) for item in str(value).split(",") if item.strip()]


def parse_args():
    parser = argparse.ArgumentParser(description="Monitor Vinted listings with optional wrapper, filters, and proxies.")
    parser.add_argument("--cli", action="store_true", help="Run in terminal mode instead of opening the GUI")
    parser.add_argument("--domain", default="fr", help="Vinted domain like fr, de, or com")
    parser.add_argument("--query", default="carhartt jacket", help="Search text to watch")
    parser.add_argument("--search-url", default=None, help="Optional prebuilt Vinted search URL")
    parser.add_argument("--per-page", type=int, default=20, help="How many items to fetch per poll")
    parser.add_argument("--min-delay", type=float, default=10, help="Minimum delay between polls")
    parser.add_argument("--max-delay", type=float, default=20, help="Maximum delay between polls")
    parser.add_argument("--catalog-ids", default=None, help="Comma-separated catalog IDs")
    parser.add_argument("--brand-ids", default=None, help="Comma-separated brand IDs")
    parser.add_argument("--price-from", type=float, default=None, help="Minimum price filter")
    parser.add_argument("--price-to", type=float, default=None, help="Maximum price filter")
    parser.add_argument("--proxy", default=None, help="Single proxy URL, for example http://user:pass@host:port")
    parser.add_argument("--proxies", default=None, help="Comma-separated proxy URLs")
    parser.add_argument("--proxies-file", default=None, help="File with one proxy URL per line")
    parser.add_argument("--no-wrapper", action="store_true", help="Force the built-in HTTP fallback instead of the wrapper")
    parser.add_argument("--discord-webhook-url", default=None, help="Discord webhook URL for alerts")
    parser.add_argument("--telegram-bot-token", default=None, help="Telegram bot token for alerts")
    parser.add_argument("--telegram-chat-id", default=None, help="Telegram chat ID for alerts")
    parser.add_argument("--no-enrich", action="store_true", help="Skip item_info lookups for each new listing")
    return parser.parse_args()


def load_proxies_from_file(path):
    if not path:
        return None
    with open(path, "r", encoding="utf-8") as file_handle:
        return [line.strip() for line in file_handle if line.strip()]


class VintedScraperGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Vinted Scraper")
        self.root.geometry("980x760")
        self.root.minsize(900, 680)

        self.scraper = None
        self.worker_thread = None
        self.stop_event = threading.Event()

        self.domain_var = tk.StringVar(value="fr")
        self.query_var = tk.StringVar(value="carhartt jacket")
        self.search_url_var = tk.StringVar(value="")
        self.price_from_var = tk.StringVar(value="")
        self.price_to_var = tk.StringVar(value="")
        self.min_delay_var = tk.StringVar(value="10")
        self.max_delay_var = tk.StringVar(value="20")
        self.per_page_var = tk.StringVar(value="20")
        self.catalog_ids_var = tk.StringVar(value="")
        self.brand_ids_var = tk.StringVar(value="")
        self.proxy_var = tk.StringVar(value="")
        self.discord_var = tk.StringVar(value="")
        self.telegram_token_var = tk.StringVar(value="")
        self.telegram_chat_var = tk.StringVar(value="")
        self.use_wrapper_var = tk.BooleanVar(value=False)
        self.enrich_var = tk.BooleanVar(value=True)

        self._build_ui()

    def _build_ui(self):
        container = tk.Frame(self.root, padx=12, pady=12)
        container.pack(fill="both", expand=True)

        form = tk.LabelFrame(container, text="Search Settings", padx=10, pady=10)
        form.pack(fill="x")

        self._add_row(form, 0, "Domain", self.domain_var, "fr")
        self._add_row(form, 1, "Search for", self.query_var, "carhartt jacket")
        self._add_row(form, 2, "Search URL", self.search_url_var, "optional")
        self._add_row(form, 3, "Price from", self.price_from_var, "10")
        self._add_row(form, 4, "Price to", self.price_to_var, "200")
        self._add_row(form, 5, "Catalog IDs", self.catalog_ids_var, "1,2,3")
        self._add_row(form, 6, "Brand IDs", self.brand_ids_var, "100,200")
        self._add_row(form, 7, "Per page", self.per_page_var, "20")
        self._add_row(form, 8, "Min delay", self.min_delay_var, "10")
        self._add_row(form, 9, "Max delay", self.max_delay_var, "20")

        proxy_box = tk.LabelFrame(container, text="Proxy Settings", padx=10, pady=10)
        proxy_box.pack(fill="x", pady=(10, 0))
        self._add_row(proxy_box, 0, "Single proxy", self.proxy_var, "http://user:pass@host:port")

        proxy_button_row = tk.Frame(proxy_box)
        proxy_button_row.grid(row=1, column=0, columnspan=3, sticky="w", pady=(4, 0))
        tk.Button(proxy_button_row, text="Load Proxy File", command=self.load_proxy_file).pack(side="left")
        self.proxy_file_label = tk.Label(proxy_button_row, text="No proxy file loaded", anchor="w")
        self.proxy_file_label.pack(side="left", padx=(10, 0))

        alerts_box = tk.LabelFrame(container, text="Alerts", padx=10, pady=10)
        alerts_box.pack(fill="x", pady=(10, 0))
        self._add_row(alerts_box, 0, "Discord webhook", self.discord_var, "optional")
        self._add_row(alerts_box, 1, "Telegram token", self.telegram_token_var, "optional")
        self._add_row(alerts_box, 2, "Telegram chat ID", self.telegram_chat_var, "optional")

        options_row = tk.Frame(container)
        options_row.pack(fill="x", pady=(10, 0))
        tk.Checkbutton(options_row, text="Use vinted-api-wrapper", variable=self.use_wrapper_var).pack(side="left")
        tk.Checkbutton(options_row, text="Enrich new listings", variable=self.enrich_var).pack(side="left", padx=(12, 0))

        button_row = tk.Frame(container)
        button_row.pack(fill="x", pady=(10, 0))
        self.start_button = tk.Button(button_row, text="Start Monitoring", command=self.start_monitoring, height=2, width=18)
        self.start_button.pack(side="left")
        self.stop_button = tk.Button(button_row, text="Stop", command=self.stop_monitoring, height=2, width=10, state="disabled")
        self.stop_button.pack(side="left", padx=(10, 0))
        tk.Button(button_row, text="Clear Log", command=self.clear_log, height=2, width=10).pack(side="left", padx=(10, 0))

        log_box = tk.LabelFrame(container, text="Live Log", padx=10, pady=10)
        log_box.pack(fill="both", expand=True, pady=(10, 0))
        self.log_text = scrolledtext.ScrolledText(log_box, wrap=tk.WORD, height=18)
        self.log_text.pack(fill="both", expand=True)
        self.log_text.configure(state="disabled")

    def _add_row(self, parent, row, label, variable, placeholder=""):
        tk.Label(parent, text=label, width=16, anchor="w").grid(row=row, column=0, sticky="w", pady=4)
        entry = tk.Entry(parent, textvariable=variable, width=70)
        entry.grid(row=row, column=1, sticky="we", pady=4)
        parent.grid_columnconfigure(1, weight=1)
        tk.Label(parent, text=placeholder, fg="#666666", anchor="w").grid(row=row, column=2, sticky="w", padx=(8, 0))
        return entry

    def load_proxy_file(self):
        path = filedialog.askopenfilename(title="Select proxy file", filetypes=[("Text files", "*.txt"), ("All files", "*.*")])
        if not path:
            return
        self.proxy_file_path = path
        self.proxy_file_label.configure(text=path)

    def append_log(self, message):
        def write():
            self.log_text.configure(state="normal")
            self.log_text.insert(tk.END, message + "\n")
            self.log_text.see(tk.END)
            self.log_text.configure(state="disabled")

        self.root.after(0, write)

    def clear_log(self):
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", tk.END)
        self.log_text.configure(state="disabled")

    def _parse_optional_float(self, value):
        value = value.strip()
        return float(value) if value else None

    def _parse_optional_ints(self, value):
        value = value.strip()
        if not value:
            return None
        return parse_csv_ints(value)

    def _build_proxy_args(self):
        proxies = None
        if hasattr(self, "proxy_file_path") and self.proxy_file_path:
            proxies = load_proxies_from_file(self.proxy_file_path)
        return self.proxy_var.get().strip() or None, proxies

    def start_monitoring(self):
        if self.worker_thread and self.worker_thread.is_alive():
            messagebox.showinfo("Vinted Scraper", "Monitoring is already running.")
            return

        try:
            price_from = self._parse_optional_float(self.price_from_var.get())
            price_to = self._parse_optional_float(self.price_to_var.get())
            min_delay = float(self.min_delay_var.get().strip() or "10")
            max_delay = float(self.max_delay_var.get().strip() or "20")
            per_page = int(self.per_page_var.get().strip() or "20")
            catalog_ids = self._parse_optional_ints(self.catalog_ids_var.get())
            brand_ids = self._parse_optional_ints(self.brand_ids_var.get())
            proxy, proxies = self._build_proxy_args()
        except ValueError as exc:
            messagebox.showerror("Invalid input", f"Please check the numeric fields:\n{exc}")
            return

        self.stop_event.clear()
        self.scraper = ContinuousVintedScraper(
            domain=self.domain_var.get().strip() or "fr",
            proxy=proxy,
            proxies=proxies,
            use_wrapper=self.use_wrapper_var.get(),
            discord_webhook_url=self.discord_var.get().strip() or None,
            telegram_bot_token=self.telegram_token_var.get().strip() or None,
            telegram_chat_id=self.telegram_chat_var.get().strip() or None,
            log_callback=self.append_log,
        )

        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")

        def worker():
            try:
                self.scraper.start_monitoring(
                    search_text=self.query_var.get().strip() or "nike",
                    search_url=self.search_url_var.get().strip() or None,
                    min_delay=min_delay,
                    max_delay=max_delay,
                    per_page=per_page,
                    catalog_ids=catalog_ids,
                    brand_ids=brand_ids,
                    price_from=price_from,
                    price_to=price_to,
                    enrich_new_items=self.enrich_var.get() and self.use_wrapper_var.get(),
                    stop_event=self.stop_event,
                )
            finally:
                self.root.after(0, self._monitor_finished)

        self.worker_thread = threading.Thread(target=worker, daemon=True)
        self.worker_thread.start()

    def _monitor_finished(self):
        self.start_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        self.append_log("[GUI] Monitoring stopped.")

    def stop_monitoring(self):
        self.stop_event.set()
        self.append_log("[GUI] Stop requested.")


def run_cli(args):
    file_proxies = load_proxies_from_file(args.proxies_file)
    monitor = ContinuousVintedScraper(
        domain=args.domain,
        proxy=args.proxy,
        proxies=file_proxies if file_proxies else args.proxies,
        use_wrapper=not args.no_wrapper,
        discord_webhook_url=args.discord_webhook_url,
        telegram_bot_token=args.telegram_bot_token,
        telegram_chat_id=args.telegram_chat_id,
    )

    monitor.start_monitoring(
        search_text=args.query,
        search_url=args.search_url,
        min_delay=args.min_delay,
        max_delay=args.max_delay,
        per_page=args.per_page,
        catalog_ids=parse_csv_ints(args.catalog_ids),
        brand_ids=parse_csv_ints(args.brand_ids),
        price_from=args.price_from,
        price_to=args.price_to,
        enrich_new_items=not args.no_enrich,
    )

if __name__ == "__main__":
    args = parse_args()
    if args.cli:
        run_cli(args)
    else:
        root = tk.Tk()
        app = VintedScraperGUI(root)
        root.mainloop()