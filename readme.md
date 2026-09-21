# vinted listing monitor

a small python tool for watching vinted searches and getting notified when something new appears. it has a tkinter gui for clicking around and a cli mode for when you want the terminal to do the work.

![python](https://img.shields.io/badge/python-3.x-blue.svg)

---

## features

- **continuous monitoring:** keeps checking a vinted catalog search for new listings.
- **search filters:** search by text, domain, price range, catalog ids, brand ids, and page size.
- **new listing detection:** remembers listings it has already seen so you do not get the same alert every few seconds.
- **discord alerts:** send new listing messages to a discord webhook.
- **telegram alerts:** send the same kind of messages to a telegram chat.
- **optional item details:** use the [`vinted-api-wrapper`](https://pypi.org/project/vinted-api-wrapper/) package to fetch extra details like the seller.
- **proxy support:** use one proxy, several proxies, a proxy file, or environment variables.
- **two ways to run it:** use the gui or pass `--cli` to run it from a terminal.
- **random polling delay:** adds a random delay between checks so the program is not hammering the site on a perfectly predictable schedule.

---

## quick disclaimer

this project is for personal experimentation and learning. make sure your use of it follows vinted's terms, robots rules, and any local laws that apply. do not use it to spam the site or anyone else.

also, keep webhook urls, bot tokens, proxy credentials, and other private settings out of public repos. future-you will be grateful.

---

## quickstart

### prerequisites

you will need python 3.x installed.

install the required packages with pip:

```bash
pip install curl-cffi vinted-api-wrapper
```

### run the gui

```bash
python VintedScraper.py
```

fill in the search settings, optionally add alert details or proxies, then hit **Start Monitoring**. the live log will show what the scraper is doing.

### run from the cli

```bash
python VintedScraper.py --cli --domain fr --query "carhartt jacket"
```

some useful options:

```bash
python VintedScraper.py --cli \
  --domain de \
  --query "nike hoodie" \
  --price-from 10 \
  --price-to 100 \
  --min-delay 10 \
  --max-delay 20 \
  --discord-webhook-url "your-webhook-url"
```

use `python VintedScraper.py --help` to see the full list of cli options.

---

## using the wrapper

[`vinted-api-wrapper`](https://pypi.org/project/vinted-api-wrapper/) is optional in the sense that the scraper can still read catalog listings without it. when enabled, the program uses it to look up extra information for new items, such as the seller.

in the gui, tick **use vinted-api-wrapper** and **enrich new listings**. from the cli, wrapper use is enabled by default; add `--no-wrapper` to turn it off, or `--no-enrich` to skip the extra item lookups.

---

## proxies

### one proxy

```bash
python VintedScraper.py --cli --proxy "http://user:pass@host:port"
```

### several proxies

```bash
python VintedScraper.py --cli --proxies "http://proxy-one:8080,http://proxy-two:8080"
```

### proxy file

put one proxy url on each line in a text file, then run:

```bash
python VintedScraper.py --cli --proxies-file proxies.txt
```

you can also use `VINTED_PROXY` for one proxy or `VINTED_PROXIES` for multiple proxies separated by commas or new lines.

---

## alerts

for discord, use a webhook url:

```bash
python VintedScraper.py --cli --discord-webhook-url "your-webhook-url"
```

for telegram, provide the bot token and chat id:

```bash
python VintedScraper.py --cli \
  --telegram-bot-token "your-bot-token" \
  --telegram-chat-id "your-chat-id"
```

new listing alerts include the title, price, currency, link, and any available brand, size, or seller information.
