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
| `DOMAIN`, `EMAIL` | (ঐচ্ছিক, পরে চাইলে) ডোমেইন থাকলে ফ্রি HTTPS চালু হবে | দরকার নেই |

## Google Drive কানেক্ট

প্যানেলের **Google Drive** ট্যাবে যান → **Google লগইন লিংক তৈরি করো** → Google-এ Allow দিন →
যে পেজটা খুলবে না (127.0.0.1...) তার address bar-এর পুরো লিংক কপি করে প্যানেলে পেস্ট করুন → **কানেক্ট**।

## আনইনস্টল

```bash
sudo bash install.sh --uninstall
```

(ডাউনলোড করা ফাইলগুলো মুছবে না।)

## ফাইলগুলো

- `install.sh`: পুরো সেটআপ স্ক্রিপ্ট
- `panel/server.py`: প্যানেলের ব্যাকএন্ড (শুধু Python, বাড়তি কিছু লাগে না)
- `panel/index.html`: প্যানেলের ওয়েব পেজ
- `docs/guide.html`: সেটআপ আর ব্যবহারের পুরো গাইড
