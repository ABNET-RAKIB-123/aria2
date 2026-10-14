# aria2
Download Or Upload

Ubuntu সার্ভারে **এক কমান্ডে** aria2 + নিজস্ব ওয়েব প্যানেল সেটআপ। প্যানেল থেকে সব কাজ:

- **ডাউনলোড**: Direct link, Magnet, Torrent, Google Drive শেয়ার লিংক → সার্ভারে
- **সরাসরি Drive-এ**: Direct link → সার্ভারে না রেখেই Google Drive-এ
- **সার্ভারের ফাইল**: পিসি থেকে আপলোড (drag & drop), পিসিতে নামানো, ব্রাউজারে ভিডিও/ছবি দেখা, rename, delete, ৭ দিনের Direct link শেয়ার
- **Google Drive**: ব্রাউজার থেকে কানেক্ট, ফাইল দেখা, পিসি → Drive আপলোড, Drive → পিসি ডাউনলোড, সার্ভার ⇄ Drive ট্রান্সফার
- **অটো আপলোড**: সার্ভারে ডাউনলোড শেষ হলেই Drive-এ পাঠানো (চাইলে সার্ভার থেকে মুছে দেওয়া)
- পাসওয়ার্ড দিয়ে লগইন, মোবাইলে চলে, Dark mode

📖 **পুরো বাংলা গাইড (ছবি সহ):** [`docs/guide.html`](docs/guide.html), ডাউনলোড করে ব্রাউজারে খুলুন।

## ইনস্টল

```bash
curl -fsSL https://raw.githubusercontent.com/ABNET-RAKIB-123/aria2/main/install.sh | sudo bash
```

**ডোমেইন লাগবে না।** প্যানেল চলবে সার্ভারের IP দিয়ে: `http://SERVER-IP/`

শেষে স্ক্রিনে প্যানেলের লিংক আর পাসওয়ার্ড দেখাবে (`/root/aria2-web-credentials.txt` ফাইলেও থাকবে)।

## অপশন (environment variable)

| ভেরিয়েবল | কাজ | ডিফল্ট |
|---|---|---|
| `DOWNLOAD_DIR` | ফাইল কোন ফোল্ডারে থাকবে | `/srv/downloads` |
| `PANEL_PASSWORD` | প্যানেলের পাসওয়ার্ড | প্রথমবার নিজে থেকে তৈরি হবে |
| `REF` | কোন branch থেকে প্যানেলের ফাইল নামাবে | `main` |
| `DOMAIN`, `EMAIL` | (ঐচ্ছিক, পরে চাইলে) ডোমেইন থাকলে ফ্রি HTTPS চালু হবে, নিচের [HTTPS অংশ](#-https-ঐচ্ছিক-পরে-ডোমেইন-যোগ-করবেন-যেভাবে) দেখুন। `DOMAIN=none` দিলে আবার IP-তে ফিরে যাবে | দরকার নেই |

## Google Drive কানেক্ট

প্যানেলের **Google Drive** ট্যাবে যান → **Google লগইন লিংক তৈরি করো** → Google-এ Allow দিন →
যে পেজটা খুলবে না (127.0.0.1...) তার address bar-এর পুরো লিংক কপি করে প্যানেলে পেস্ট করুন → **কানেক্ট**।

## আপডেট

ইনস্টলের কমান্ডটাই আবার চালান। ফাইল, পাসওয়ার্ড, Drive কানেকশন, আর আগে দেওয়া `DOMAIN` / `DOWNLOAD_DIR` সব মনে থাকে, আবার লিখতে হয় না:

```bash
curl -fsSL https://raw.githubusercontent.com/ABNET-RAKIB-123/aria2/main/install.sh | sudo bash
```

## 🔒 HTTPS (ঐচ্ছিক): পরে ডোমেইন যোগ করবেন যেভাবে

এখন ডোমেইন ছাড়াই চালান। প্যানেল `http://SERVER-IP/` ঠিকানায় সব কাজ করবে। পরে ডোমেইন নিলে নিচের ধাপগুলো করলেই HTTPS চালু হবে।

### HTTPS কেন?
- লগইনের পাসওয়ার্ড আর ফাইল এনক্রিপ্ট হয়ে যায়। পাবলিক WiFi থেকেও নিরাপদ থাকে।
- ব্রাউজারে "Not secure" লেখা চলে যায়।
- IP-র বদলে মনে রাখার মতো একটা ঠিকানা পান, যেমন `https://dl.example.com`।

### যা লাগবে
| জিনিস | কোথায় পাবেন | খরচ |
|---|---|---|
| একটা ডোমেইন | Namecheap, Porkbun, Cloudflare, GoDaddy, বা বাংলাদেশি রেজিস্ট্রার | বছরে প্রায় ১০০০ থেকে ১৫০০ টাকা (`.com`) |
| একটা ইমেইল | আপনার Gmail-ই চলবে | ফ্রি |
| SSL সার্টিফিকেট | Let's Encrypt, script নিজেই নিয়ে নেয় | ফ্রি |

> ডোমেইন কিনতে না চাইলে **sslip.io** দিয়ে ফ্রিতে HTTPS করা যায়। নিচে **উপায় ২** দেখুন।

### উপায় ১: নিজের ডোমেইন দিয়ে

**ধাপ ১: DNS-এ A record যোগ করুন**

ডোমেইন যেখান থেকে কিনেছেন, সেখানকার DNS সেটিংয়ে গিয়ে একটা record যোগ করুন:

| Type | Name / Host | Value / Points to | TTL |
|---|---|---|---|
| `A` | `dl` (তাহলে ঠিকানা হবে `dl.example.com`), অথবা `@` (তাহলে শুধু `example.com`) | আপনার সার্ভারের IP | Auto |

সার্ভারে IPv6 থাকলে চাইলে একটা `AAAA` record-ও দিতে পারেন। না দিলেও চলবে।

**ধাপ ২: DNS কাজ করছে কিনা দেখুন** (৫ মিনিট থেকে কয়েক ঘণ্টা লাগতে পারে)

```bash
ping dl.example.com
# অথবা
nslookup dl.example.com
```

উত্তরে আপনার সার্ভারের IP দেখালে পরের ধাপে যান। না দেখালে আরো কিছুক্ষণ অপেক্ষা করুন।

**ধাপ ৩: পোর্ট 80 আর 443 খোলা আছে কিনা দেখুন**

AWS, Oracle বা Google Cloud হলে ওদের প্যানেলে **443/TCP** পোর্টটাও খুলে দিন (80 আগে থেকেই খোলা থাকার কথা)। Let's Encrypt পোর্ট 80 দিয়ে যাচাই করে, তাই ওটাও খোলা থাকতে হবে। সার্ভারের ufw-তে script নিজেই দুইটা খুলে দেয়।

**ধাপ ৪: এই একটা কমান্ড চালান**

```bash
curl -fsSL https://raw.githubusercontent.com/ABNET-RAKIB-123/aria2/main/install.sh | sudo DOMAIN=dl.example.com EMAIL=you@gmail.com bash
```

`dl.example.com` আর `you@gmail.com` বদলে নিজেরগুলো দিন। script নিজে থেকে:
- Let's Encrypt থেকে ফ্রি সার্টিফিকেট নেয়
- nginx-এ HTTPS চালু করে, আর `http://` দিয়ে কেউ ঢুকলে `https://`-এ পাঠিয়ে দেয়
- ডোমেইনটা মনে রাখে, পরে আপডেটের সময় আবার লিখতে হয় না

**ধাপ ৫: নতুন ঠিকানায় খুলুন**

`https://dl.example.com/`। পাসওয়ার্ড, ফাইল আর Drive কানেকশন আগের মতোই থাকবে।

> ⚠️ HTTPS চালু হওয়ার পর `http://SERVER-IP/` দিয়ে আর ঢোকা যাবে না, শুধু ডোমেইন দিয়ে খুলবে।

### উপায় ২: ডোমেইন না কিনে, ফ্রি (sslip.io)

sslip.io যেকোনো IP থেকে একটা নাম বানিয়ে দেয়, DNS-এ কিছু করতে হয় না। IP-র ডট (`.`) বদলে ড্যাশ (`-`) দিন। যেমন IP `203.0.113.10` হলে:

```bash
curl -fsSL https://raw.githubusercontent.com/ABNET-RAKIB-123/aria2/main/install.sh | sudo DOMAIN=203-0-113-10.sslip.io EMAIL=you@gmail.com bash
```

প্যানেল খুলবে `https://203-0-113-10.sslip.io/` ঠিকানায়। ঠিকানাটা মনে রাখা একটু কঠিন, কিন্তু পুরোপুরি ফ্রি। (Let's Encrypt-এর লিমিটের কারণে কখনো কখনো সার্টিফিকেট পেতে দেরি হতে পারে। তখন কিছুক্ষণ পরে আবার চালান।)

### Cloudflare ব্যবহার করলে
- DNS record-এর **Proxy status** প্রথমে **DNS only** (ধূসর মেঘ ☁️) রাখুন, তারপর script চালান।
- সার্টিফিকেট পাওয়ার পর চাইলে **Proxied** (কমলা মেঘ 🟠) করতে পারেন। তখন Cloudflare-এর **SSL/TLS → Overview** থেকে মোড **Full (strict)** করে দিন।
- ⚠️ Proxied অবস্থায় Cloudflare ফ্রি প্ল্যানে একবারে প্রায় ১০০ MB-র বেশি আপলোড করতে দেয় না। বড় ফাইল আপলোড করলে **DNS only** রাখাই ভালো।

### পরে যা যা করা যায়

| কী করতে চান | কমান্ড |
|---|---|
| আপডেট (ডোমেইন মনে থাকবে) | `curl -fsSL https://raw.githubusercontent.com/ABNET-RAKIB-123/aria2/main/install.sh \| sudo bash` |
| ডোমেইন বদলানো | `... \| sudo DOMAIN=new.example.com EMAIL=you@gmail.com bash` |
| HTTPS বন্ধ করে আবার IP-তে ফেরা | `... \| sudo DOMAIN=none bash` |
| সার্টিফিকেটের অবস্থা দেখা | `sudo certbot certificates` |
| রিনিউ ঠিকমতো হবে কিনা পরীক্ষা | `sudo certbot renew --dry-run` |

সার্টিফিকেট ৯০ দিনের হয়, কিন্তু certbot নিজে থেকেই রিনিউ করে। আপনাকে কিছু করতে হবে না।

### সমস্যা হলে (certbot failed)

script শেষে `certbot failed` দেখালে প্যানেল HTTP-তেই চলতে থাকবে, কিছু নষ্ট হবে না। এই জিনিসগুলো দেখুন, তারপর একই কমান্ড আবার চালান:

1. **DNS এখনো ছড়ায়নি:** `ping dl.example.com` চালিয়ে দেখুন আপনার সার্ভারের IP দেখায় কিনা।
2. **পোর্ট 80 বন্ধ:** ক্লাউড প্যানেলের ফায়ারওয়াল দেখুন। Oracle Cloud হলে সার্ভারের ভেতরের iptables-ও খুলতে হয় (গাইডের "সমস্যা সমাধান" অংশ দেখুন)।
3. **Cloudflare Proxied চালু:** **DNS only** করে আবার চালান।
4. **অনেকবার চেষ্টা করেছেন:** Let's Encrypt একই ডোমেইনে ঘণ্টায় ৫ বারের বেশি ব্যর্থ চেষ্টা করতে দেয় না। এক ঘণ্টা অপেক্ষা করুন।

বিস্তারিত এরর দেখতে: `sudo tail -n 50 /var/log/letsencrypt/letsencrypt.log`

## আনইনস্টল

```bash
curl -fsSL https://raw.githubusercontent.com/ABNET-RAKIB-123/aria2/main/install.sh | sudo bash -s -- --uninstall
```

(ডাউনলোড করা ফাইলগুলো মুছবে না।)

## ফাইলগুলো

- `install.sh`: পুরো সেটআপ স্ক্রিপ্ট
- `panel/server.py`: প্যানেলের ব্যাকএন্ড (শুধু Python, বাড়তি কিছু লাগে না)
- `panel/index.html`: প্যানেলের ওয়েব পেজ
- `docs/guide.html`: সেটআপ আর ব্যবহারের পুরো গাইড
